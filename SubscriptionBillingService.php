<?php

namespace App\Services;

use App\Models\User;
use App\Models\Plan;
use App\Models\Subscription;
use App\Models\Invoice;
use App\Models\InvoiceItem;
use App\Models\PaymentMethod;
use App\Models\UsageRecord;
use App\Models\Coupon;
use App\Models\CreditBalance;
use App\Events\SubscriptionCreated;
use App\Events\SubscriptionCancelled;
use App\Events\SubscriptionResumed;
use App\Events\InvoiceGenerated;
use App\Events\PaymentFailed;
use App\Jobs\SendInvoiceEmail;
use App\Jobs\ProcessWebhook;
use App\Jobs\RetryPayment;
use App\Exceptions\BillingException;
use App\Exceptions\InsufficientFundsException;
use App\Contracts\PaymentProcessorInterface;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Carbon;
use Illuminate\Support\Str;
use Illuminate\Support\Collection;

class SubscriptionBillingService
{
    protected PaymentProcessorInterface $paymentProcessor;
    protected array $config;

    private const MAX_RETRY_ATTEMPTS = 3;
    private const GRACE_PERIOD_DAYS = 7;
    private const PRORATION_PRECISION = 8;
    private const INVOICE_DUE_DAYS = 30;

    public function __construct(PaymentProcessorInterface $paymentProcessor)
    {
        $this->paymentProcessor = $paymentProcessor;
        $this->config = config('billing', []);
    }

    // ─── Subscription Creation ────────────────────────────────────────

    /**
     * Create a new subscription for a user
     */
    public function createSubscription(
        User $user,
        Plan $plan,
        ?string $couponCode = null,
        ?string $paymentMethodId = null,
        array $options = []
    ): Subscription {
        $this->validateCanSubscribe($user, $plan);

        $paymentMethod = $this->resolvePaymentMethod($user, $paymentMethodId);

        DB::beginTransaction();

        try {
            $trialEndsAt = null;
            if ($plan->trial_days > 0 && !$user->hasUsedTrial()) {
                $trialEndsAt = now()->addDays($plan->trial_days);
            }

            $discount = 0;
            $coupon = null;
            if ($couponCode) {
                $coupon = $this->validateAndApplyCoupon($couponCode, $plan);
                $discount = $this->calculateCouponDiscount($coupon, $plan->price);
            }

            $billingCycleStart = now();
            $billingCycleEnd = $this->calculateBillingCycleEnd(
                $billingCycleStart,
                $plan->billing_interval
            );

            $subscription = Subscription::create([
                'user_id'              => $user->id,
                'plan_id'              => $plan->id,
                'status'               => $trialEndsAt ? 'trialing' : 'active',
                'payment_method_id'    => $paymentMethod->id,
                'coupon_id'            => $coupon?->id,
                'trial_ends_at'        => $trialEndsAt,
                'current_period_start' => $billingCycleStart,
                'current_period_end'   => $billingCycleEnd,
                'cancel_at_period_end' => false,
                'quantity'             => $options['quantity'] ?? 1,
                'metadata'             => $options['metadata'] ?? [],
                'tax_rate'             => $this->getTaxRate($user),
            ]);

            if (!$trialEndsAt) {
                $amount = ($plan->price * $subscription->quantity) - $discount;
                $this->chargeSubscription($subscription, $amount, 'initial');
            }

            if ($coupon) {
                $coupon->increment('times_used');
            }

            $this->clearUserCache($user);

            DB::commit();

            event(new SubscriptionCreated($subscription));

            Log::info('Subscription created', [
                'user_id'         => $user->id,
                'plan_id'         => $plan->id,
                'subscription_id' => $subscription->id,
            ]);

            return $subscription;

        } catch (\Exception $e) {
            DB::rollBack();
            Log::error('Subscription creation failed', [
                'user_id' => $user->id,
                'plan_id' => $plan->id,
                'error'   => $e->getMessage(),
            ]);
            throw new BillingException("Failed to create subscription: {$e->getMessage()}");
        }
    }

    // ─── Plan Changes ─────────────────────────────────────────────────

    /**
     * Change subscription to a different plan (upgrade/downgrade)
     */
    public function changePlan(
        Subscription $subscription,
        Plan $newPlan,
        bool $prorate = true
    ): Subscription {
        $currentPlan = $subscription->plan;

        if ($currentPlan->id === $newPlan->id) {
            throw new BillingException('Already subscribed to this plan');
        }

        $this->validatePlanCompatibility($currentPlan, $newPlan);

        DB::beginTransaction();

        try {
            $prorationAmount = 0;

            if ($prorate && $subscription->status === 'active') {
                $prorationAmount = $this->calculateProration(
                    $subscription,
                    $currentPlan,
                    $newPlan
                );
            }

            $oldPlanId = $subscription->plan_id;
            $subscription->plan_id = $newPlan->id;

            $isUpgrade = $newPlan->price > $currentPlan->price;

            if ($isUpgrade) {
                $subscription->current_period_start = now();
                $subscription->current_period_end = $this->calculateBillingCycleEnd(
                    now(),
                    $newPlan->billing_interval
                );
            }

            $subscription->save();

            if ($prorationAmount > 0) {
                $this->chargeSubscription($subscription, $prorationAmount, 'proration');
            } elseif ($prorationAmount < 0) {
                $this->createCredit(
                    $subscription->user,
                    abs($prorationAmount),
                    "Plan change credit: {$currentPlan->name} → {$newPlan->name}"
                );
            }

            $this->logSubscriptionEvent($subscription, 'plan_changed', [
                'old_plan_id'      => $oldPlanId,
                'new_plan_id'      => $newPlan->id,
                'proration_amount' => $prorationAmount,
            ]);

            $this->clearUserCache($subscription->user);

            DB::commit();

            return $subscription->fresh();

        } catch (\Exception $e) {
            DB::rollBack();
            throw new BillingException("Plan change failed: {$e->getMessage()}");
        }
    }

    /**
     * Update subscription quantity (e.g., seat count)
     */
    public function updateQuantity(Subscription $subscription, int $newQuantity): Subscription
    {
        if ($newQuantity < 1) {
            throw new BillingException('Quantity must be at least 1');
        }

        $plan = $subscription->plan;
        if ($plan->max_quantity && $newQuantity > $plan->max_quantity) {
            throw new BillingException("Maximum quantity for this plan is {$plan->max_quantity}");
        }

        $oldQuantity = $subscription->quantity;
        $quantityDiff = $newQuantity - $oldQuantity;

        if ($quantityDiff == 0) {
            return $subscription;
        }

        DB::beginTransaction();

        try {
            $subscription->quantity = $newQuantity;
            $subscription->save();

            if ($quantityDiff > 0) {
                $remainingDays = now()->diffInDays($subscription->current_period_end);
                $totalDays = $subscription->current_period_start->diffInDays(
                    $subscription->current_period_end
                );

                $dailyRate = $plan->price / $totalDays;
                $prorationCharge = $dailyRate * $quantityDiff * $remainingDays;

                if ($prorationCharge > 0.50) {
                    $this->chargeSubscription(
                        $subscription,
                        round($prorationCharge, 2),
                        'quantity_increase'
                    );
                }
            }

            $this->logSubscriptionEvent($subscription, 'quantity_changed', [
                'old_quantity' => $oldQuantity,
                'new_quantity' => $newQuantity,
            ]);

            DB::commit();
            return $subscription->fresh();

        } catch (\Exception $e) {
            DB::rollBack();
            throw new BillingException("Quantity update failed: {$e->getMessage()}");
        }
    }

    // ─── Cancellation & Resume ────────────────────────────────────────

    /**
     * Cancel subscription (at period end or immediately)
     */
    public function cancelSubscription(
        Subscription $subscription,
        bool $immediately = false,
        ?string $reason = null
    ): Subscription {
        if ($subscription->status === 'cancelled') {
            throw new BillingException('Subscription is already cancelled');
        }

        DB::beginTransaction();

        try {
            if ($immediately) {
                $subscription->status = 'cancelled';
                $subscription->cancelled_at = now();
                $subscription->ends_at = now();

                $remainingValue = $this->calculateRemainingValue($subscription);
                if ($remainingValue > 1.00) {
                    $this->createCredit(
                        $subscription->user,
                        $remainingValue,
                        'Subscription cancellation refund'
                    );
                }
            } else {
                $subscription->cancel_at_period_end = true;
                $subscription->cancelled_at = now();
                $subscription->ends_at = $subscription->current_period_end;
            }

            $subscription->cancellation_reason = $reason;
            $subscription->save();

            $this->logSubscriptionEvent($subscription, 'cancelled', [
                'immediately' => $immediately,
                'reason'      => $reason,
            ]);

            $this->clearUserCache($subscription->user);

            DB::commit();

            event(new SubscriptionCancelled($subscription));

            return $subscription;

        } catch (\Exception $e) {
            DB::rollBack();
            throw new BillingException("Cancellation failed: {$e->getMessage()}");
        }
    }

    /**
     * Resume a cancelled subscription (before period end)
     */
    public function resumeSubscription(Subscription $subscription): Subscription
    {
        if (!$subscription->cancel_at_period_end) {
            throw new BillingException('Subscription is not pending cancellation');
        }

        if ($subscription->ends_at && $subscription->ends_at->isPast()) {
            throw new BillingException('Subscription has already ended');
        }

        $subscription->cancel_at_period_end = false;
        $subscription->cancelled_at = null;
        $subscription->ends_at = null;
        $subscription->cancellation_reason = null;
        $subscription->save();

        $this->logSubscriptionEvent($subscription, 'resumed');
        $this->clearUserCache($subscription->user);

        event(new SubscriptionResumed($subscription));

        return $subscription;
    }

    // ─── Billing & Invoicing ──────────────────────────────────────────

    /**
     * Process recurring billing for all active subscriptions
     */
    public function processRecurringBilling(): array
    {
        $results = ['success' => 0, 'failed' => 0, 'skipped' => 0, 'errors' => []];

        $subscriptions = Subscription::where('status', 'active')
            ->where('current_period_end', '<=', now())
            ->where('cancel_at_period_end', false)
            ->with(['user', 'plan', 'paymentMethod'])
            ->cursor();

        foreach ($subscriptions as $subscription) {
            try {
                if (!$subscription->plan || !$subscription->user) {
                    $results['skipped']++;
                    continue;
                }

                $this->renewSubscription($subscription);
                $results['success']++;

            } catch (InsufficientFundsException $e) {
                $this->handlePaymentFailure($subscription, $e);
                $results['failed']++;
                $results['errors'][] = [
                    'subscription_id' => $subscription->id,
                    'error'           => $e->getMessage(),
                ];

            } catch (\Exception $e) {
                $results['failed']++;
                $results['errors'][] = [
                    'subscription_id' => $subscription->id,
                    'error'           => $e->getMessage(),
                ];
                Log::error('Billing processing error', [
                    'subscription_id' => $subscription->id,
                    'error'           => $e->getMessage(),
                ]);
            }
        }

        $this->processEndedSubscriptions();

        Log::info('Recurring billing completed', $results);
        return $results;
    }

    /**
     * Renew a single subscription
     */
    protected function renewSubscription(Subscription $subscription): Invoice
    {
        $plan = $subscription->plan;

        $newPeriodStart = $subscription->current_period_end;
        $newPeriodEnd = $this->calculateBillingCycleEnd(
            $newPeriodStart,
            $plan->billing_interval
        );

        $baseAmount = $plan->price * $subscription->quantity;
        $discount = 0;

        if ($subscription->coupon_id) {
            $coupon = Coupon::find($subscription->coupon_id);
            if ($coupon && $this->isCouponStillValid($coupon, $subscription)) {
                $discount = $this->calculateCouponDiscount($coupon, $baseAmount);
            } else {
                $subscription->coupon_id = null;
            }
        }

        $credit = $this->getAvailableCredit($subscription->user);
        $creditApplied = 0;
        if ($credit > 0) {
            $creditApplied = min($credit, $baseAmount - $discount);
            $this->deductCredit($subscription->user, $creditApplied);
        }

        $taxableAmount = $baseAmount - $discount - $creditApplied;
        $tax = $taxableAmount * ($subscription->tax_rate / 100);
        $totalAmount = $taxableAmount + $tax;

        DB::beginTransaction();

        try {
            $invoice = $this->createInvoice($subscription, [
                'base_amount'    => $baseAmount,
                'discount'       => $discount,
                'credit_applied' => $creditApplied,
                'tax'            => round($tax, 2),
                'total'          => round($totalAmount, 2),
                'period_start'   => $newPeriodStart,
                'period_end'     => $newPeriodEnd,
            ]);

            if ($totalAmount > 0) {
                $this->chargeSubscription($subscription, $totalAmount, 'renewal');
                $invoice->update(['status' => 'paid', 'paid_at' => now()]);
            } else {
                $invoice->update(['status' => 'paid', 'paid_at' => now()]);
            }

            $subscription->update([
                'current_period_start' => $newPeriodStart,
                'current_period_end'   => $newPeriodEnd,
            ]);

            DB::commit();

            SendInvoiceEmail::dispatch($invoice)->delay(now()->addMinutes(5));
            event(new InvoiceGenerated($invoice));

            return $invoice;

        } catch (\Exception $e) {
            DB::rollBack();
            throw $e;
        }
    }

    /**
     * Create an invoice for a subscription charge
     */
    protected function createInvoice(Subscription $subscription, array $details): Invoice
    {
        $invoice = Invoice::create([
            'user_id'         => $subscription->user_id,
            'subscription_id' => $subscription->id,
            'invoice_number'  => $this->generateInvoiceNumber(),
            'status'          => 'pending',
            'subtotal'        => $details['base_amount'],
            'discount'        => $details['discount'],
            'credit_applied'  => $details['credit_applied'],
            'tax'             => $details['tax'],
            'total'           => $details['total'],
            'currency'        => $subscription->plan->currency ?? 'USD',
            'period_start'    => $details['period_start'],
            'period_end'      => $details['period_end'],
            'due_date'        => now()->addDays(self::INVOICE_DUE_DAYS),
        ]);

        InvoiceItem::create([
            'invoice_id'  => $invoice->id,
            'description' => "{$subscription->plan->name} ({$subscription->quantity}x)",
            'quantity'    => $subscription->quantity,
            'unit_price'  => $subscription->plan->price,
            'total'       => $details['base_amount'],
        ]);

        if ($details['discount'] > 0) {
            InvoiceItem::create([
                'invoice_id'  => $invoice->id,
                'description' => 'Coupon discount',
                'quantity'    => 1,
                'unit_price'  => -$details['discount'],
                'total'       => -$details['discount'],
            ]);
        }

        return $invoice;
    }

    // ─── Usage-Based Billing ──────────────────────────────────────────

    /**
     * Record usage for metered billing
     */
    public function recordUsage(
        Subscription $subscription,
        string $metricKey,
        float $quantity,
        ?Carbon $timestamp = null
    ): UsageRecord {
        $plan = $subscription->plan;
        $metric = collect($plan->usage_metrics ?? [])->firstWhere('key', $metricKey);

        if (!$metric) {
            throw new BillingException("Unknown metric: {$metricKey}");
        }

        $currentUsage = $this->getCurrentPeriodUsage($subscription, $metricKey);
        $newTotal = $currentUsage + $quantity;

        if (isset($metric['hard_limit']) && $newTotal > $metric['hard_limit']) {
            throw new BillingException(
                "Usage limit exceeded for {$metricKey}. " .
                "Current: {$currentUsage}, Attempted: {$quantity}, Limit: {$metric['hard_limit']}"
            );
        }

        $record = UsageRecord::create([
            'subscription_id' => $subscription->id,
            'metric_key'      => $metricKey,
            'quantity'         => $quantity,
            'recorded_at'     => $timestamp ?? now(),
            'period_start'    => $subscription->current_period_start,
            'period_end'      => $subscription->current_period_end,
        ]);

        if (isset($metric['included_quantity'])) {
            $overageThreshold = $metric['included_quantity'] * 0.8;
            if ($newTotal >= $overageThreshold && $currentUsage < $overageThreshold) {
                $this->notifyUsageThreshold($subscription, $metricKey, $newTotal, $metric);
            }
        }

        Cache::forget("usage:{$subscription->id}:{$metricKey}");

        return $record;
    }

    /**
     * Calculate usage charges for a billing period
     */
    public function calculateUsageCharges(Subscription $subscription): array
    {
        $plan = $subscription->plan;
        $metrics = $plan->usage_metrics ?? [];
        $charges = [];
        $totalOverageCharge = 0;

        foreach ($metrics as $metric) {
            $usage = $this->getCurrentPeriodUsage($subscription, $metric['key']);
            $included = $metric['included_quantity'] ?? 0;
            $overage = max(0, $usage - $included);

            $overageCharge = 0;
            if ($overage > 0 && isset($metric['overage_price'])) {
                if (isset($metric['tiered_pricing'])) {
                    $overageCharge = $this->calculateTieredOverage(
                        $overage, $metric['tiered_pricing']
                    );
                } else {
                    $overageCharge = $overage * $metric['overage_price'];
                }
            }

            $charges[] = [
                'metric_key'      => $metric['key'],
                'metric_name'     => $metric['name'] ?? $metric['key'],
                'total_usage'     => $usage,
                'included'        => $included,
                'overage'         => $overage,
                'overage_charge'  => round($overageCharge, 2),
            ];

            $totalOverageCharge += $overageCharge;
        }

        return [
            'charges'              => $charges,
            'total_overage_charge' => round($totalOverageCharge, 2),
        ];
    }

    protected function calculateTieredOverage(float $overage, array $tiers): float
    {
        $totalCharge = 0;
        $remaining = $overage;

        usort($tiers, fn($a, $b) => $a['up_to'] <=> $b['up_to']);

        $previousLimit = 0;
        foreach ($tiers as $tier) {
            $tierLimit = ($tier['up_to'] ?? PHP_INT_MAX) - $previousLimit;
            $unitsInTier = min($remaining, $tierLimit);

            if ($unitsInTier <= 0) {
                break;
            }

            $totalCharge += $unitsInTier * $tier['price'];
            $remaining -= $unitsInTier;
            $previousLimit = $tier['up_to'] ?? PHP_INT_MAX;
        }

        return $totalCharge;
    }

    // ─── Payment Processing ───────────────────────────────────────────

    /**
     * Charge a subscription
     */
    protected function chargeSubscription(
        Subscription $subscription,
        float $amount,
        string $type = 'recurring'
    ): array {
        $paymentMethod = $subscription->paymentMethod;

        if (!$paymentMethod || $paymentMethod->is_expired) {
            throw new InsufficientFundsException('No valid payment method available');
        }

        $result = $this->paymentProcessor->charge([
            'amount'            => $amount,
            'currency'          => $subscription->plan->currency ?? 'USD',
            'payment_method_id' => $paymentMethod->external_id,
            'customer_id'       => $subscription->user->external_customer_id,
            'description'       => "Subscription {$subscription->id} - {$type}",
            'metadata'          => [
                'subscription_id' => $subscription->id,
                'type'            => $type,
                'plan_name'       => $subscription->plan->name,
            ],
        ]);

        if (!$result['success']) {
            throw new InsufficientFundsException(
                $result['error'] ?? 'Payment failed'
            );
        }

        return $result;
    }

    /**
     * Handle payment failure with retry logic
     */
    protected function handlePaymentFailure(
        Subscription $subscription,
        \Exception $exception
    ): void {
        $retryCount = $subscription->payment_retry_count ?? 0;

        if ($retryCount < self::MAX_RETRY_ATTEMPTS) {
            $subscription->increment('payment_retry_count');

            $delayHours = pow(2, $retryCount) * 24;
            RetryPayment::dispatch($subscription)
                ->delay(now()->addHours($delayHours));

            Log::warning('Payment retry scheduled', [
                'subscription_id' => $subscription->id,
                'retry_count'     => $retryCount + 1,
                'next_retry'      => now()->addHours($delayHours)->toISOString(),
            ]);
        } else {
            $subscription->update([
                'status'          => 'past_due',
                'grace_period_end' => now()->addDays(self::GRACE_PERIOD_DAYS),
            ]);

            event(new PaymentFailed($subscription));

            Log::error('Max payment retries exceeded', [
                'subscription_id' => $subscription->id,
            ]);
        }
    }

    // ─── Helpers ──────────────────────────────────────────────────────

    protected function validateCanSubscribe(User $user, Plan $plan): void
    {
        $activeSubscription = $user->subscriptions()
            ->whereIn('status', ['active', 'trialing'])
            ->where('plan_id', $plan->id)
            ->first();

        if ($activeSubscription) {
            throw new BillingException('User already has an active subscription to this plan');
        }

        if ($plan->is_deprecated) {
            throw new BillingException('This plan is no longer available');
        }

        if (!$plan->is_active) {
            throw new BillingException('This plan is not active');
        }
    }

    protected function resolvePaymentMethod(User $user, ?string $paymentMethodId): PaymentMethod
    {
        if ($paymentMethodId) {
            $method = PaymentMethod::where('id', $paymentMethodId)
                ->where('user_id', $user->id)
                ->first();

            if (!$method) {
                throw new BillingException('Payment method not found');
            }

            return $method;
        }

        $defaultMethod = $user->paymentMethods()
            ->where('is_default', true)
            ->first();

        if (!$defaultMethod) {
            throw new BillingException('No default payment method found');
        }

        return $defaultMethod;
    }

    protected function validateAndApplyCoupon(string $code, Plan $plan): Coupon
    {
        $coupon = Coupon::where('code', strtoupper($code))
            ->where('is_active', true)
            ->first();

        if (!$coupon) {
            throw new BillingException('Invalid coupon code');
        }

        if ($coupon->expires_at && $coupon->expires_at->isPast()) {
            throw new BillingException('Coupon has expired');
        }

        if ($coupon->max_uses && $coupon->times_used >= $coupon->max_uses) {
            throw new BillingException('Coupon usage limit reached');
        }

        if ($coupon->min_amount && $plan->price < $coupon->min_amount) {
            throw new BillingException("Minimum plan price for this coupon is {$coupon->min_amount}");
        }

        if ($coupon->applicable_plans && !in_array($plan->id, $coupon->applicable_plans)) {
            throw new BillingException('Coupon is not valid for this plan');
        }

        return $coupon;
    }

    protected function calculateCouponDiscount(Coupon $coupon, float $amount): float
    {
        if ($coupon->type === 'percentage') {
            $discount = $amount * ($coupon->value / 100);
            if ($coupon->max_discount) {
                $discount = min($discount, $coupon->max_discount);
            }
            return round($discount, 2);
        }

        if ($coupon->type === 'fixed') {
            return min($coupon->value, $amount);
        }

        return 0;
    }

    protected function calculateProration(
        Subscription $subscription,
        Plan $oldPlan,
        Plan $newPlan
    ): float {
        $totalDays = $subscription->current_period_start->diffInDays(
            $subscription->current_period_end
        );

        if ($totalDays === 0) {
            return $newPlan->price - $oldPlan->price;
        }

        $remainingDays = now()->diffInDays($subscription->current_period_end);

        $dailyRateOld = ($oldPlan->price * $subscription->quantity) / $totalDays;
        $dailyRateNew = ($newPlan->price * $subscription->quantity) / $totalDays;

        $unusedCredit = $dailyRateOld * $remainingDays;
        $newCharge = $dailyRateNew * $remainingDays;

        return round($newCharge - $unusedCredit, self::PRORATION_PRECISION);
    }

    protected function calculateRemainingValue(Subscription $subscription): float
    {
        $totalDays = $subscription->current_period_start->diffInDays(
            $subscription->current_period_end
        );

        if ($totalDays == 0) {
            return 0;
        }

        $usedDays = $subscription->current_period_start->diffInDays(now());
        $remainingDays = $totalDays - $usedDays;

        $dailyRate = ($subscription->plan->price * $subscription->quantity) / $totalDays;

        return round($dailyRate * $remainingDays, 2);
    }

    protected function calculateBillingCycleEnd(Carbon $start, string $interval): Carbon
    {
        return match ($interval) {
            'weekly'     => $start->copy()->addWeek(),
            'monthly'    => $start->copy()->addMonth(),
            'quarterly'  => $start->copy()->addMonths(3),
            'semi_annual' => $start->copy()->addMonths(6),
            'yearly'     => $start->copy()->addYear(),
            default      => $start->copy()->addMonth(),
        };
    }

    protected function getTaxRate(User $user): float
    {
        $countryRates = [
            'US' => 0,
            'GB' => 20,
            'DE' => 19,
            'FR' => 20,
            'TR' => 20,
            'JP' => 10,
            'AU' => 10,
            'CA' => 13,
        ];

        return $countryRates[$user->country_code] ?? 0;
    }

    protected function isCouponStillValid(Coupon $coupon, Subscription $subscription): bool
    {
        if (!$coupon->is_active) {
            return false;
        }

        if ($coupon->expires_at && $coupon->expires_at->isPast()) {
            return false;
        }

        if ($coupon->duration === 'once') {
            return false;
        }

        if ($coupon->duration === 'repeating' && $coupon->duration_months) {
            $monthsSinceStart = $subscription->created_at->diffInMonths(now());
            return $monthsSinceStart < $coupon->duration_months;
        }

        return true;
    }

    protected function getCurrentPeriodUsage(Subscription $subscription, string $metricKey): float
    {
        $cacheKey = "usage:{$subscription->id}:{$metricKey}";

        return Cache::remember($cacheKey, 300, function () use ($subscription, $metricKey) {
            return UsageRecord::where('subscription_id', $subscription->id)
                ->where('metric_key', $metricKey)
                ->where('recorded_at', '>=', $subscription->current_period_start)
                ->where('recorded_at', '<=', $subscription->current_period_end)
                ->sum('quantity');
        });
    }

    protected function processEndedSubscriptions(): void
    {
        Subscription::where('cancel_at_period_end', true)
            ->where('current_period_end', '<=', now())
            ->where('status', '!=', 'cancelled')
            ->each(function (Subscription $subscription) {
                $subscription->update([
                    'status'   => 'cancelled',
                    'ends_at'  => now(),
                ]);
                $this->clearUserCache($subscription->user);
            });

        Subscription::where('status', 'past_due')
            ->whereNotNull('grace_period_end')
            ->where('grace_period_end', '<=', now())
            ->each(function (Subscription $subscription) {
                $subscription->update(['status' => 'cancelled', 'ends_at' => now()]);
                $this->clearUserCache($subscription->user);
            });
    }

    protected function createCredit(User $user, float $amount, string $description): CreditBalance
    {
        return CreditBalance::create([
            'user_id'     => $user->id,
            'amount'      => $amount,
            'description' => $description,
            'expires_at'  => now()->addYear(),
        ]);
    }

    protected function getAvailableCredit(User $user): float
    {
        return CreditBalance::where('user_id', $user->id)
            ->where('amount', '>', 0)
            ->where(function ($q) {
                $q->whereNull('expires_at')
                  ->orWhere('expires_at', '>', now());
            })
            ->sum('amount');
    }

    protected function deductCredit(User $user, float $amount): void
    {
        $credits = CreditBalance::where('user_id', $user->id)
            ->where('amount', '>', 0)
            ->where(function ($q) {
                $q->whereNull('expires_at')
                  ->orWhere('expires_at', '>', now());
            })
            ->orderBy('expires_at', 'asc')
            ->get();

        $remaining = $amount;
        foreach ($credits as $credit) {
            if ($remaining <= 0) break;

            $deduction = min($remaining, $credit->amount);
            $credit->decrement('amount', $deduction);
            $remaining -= $deduction;
        }
    }

    protected function validatePlanCompatibility(Plan $current, Plan $new): void
    {
        if ($current->billing_interval !== $new->billing_interval) {
            if (!($current->allow_interval_change ?? true)) {
                throw new BillingException('Cannot change billing interval on this plan');
            }
        }
    }

    protected function notifyUsageThreshold(
        Subscription $subscription,
        string $metricKey,
        float $currentUsage,
        array $metric
    ): void {
        Log::info('Usage threshold reached', [
            'subscription_id' => $subscription->id,
            'metric'          => $metricKey,
            'usage'           => $currentUsage,
            'limit'           => $metric['included_quantity'] ?? 'unlimited',
        ]);
    }

    protected function generateInvoiceNumber(): string
    {
        $prefix = 'INV';
        $date = now()->format('Ym');
        $sequence = Invoice::where('invoice_number', 'LIKE', "{$prefix}-{$date}-%")->count() + 1;
        return sprintf('%s-%s-%04d', $prefix, $date, $sequence);
    }

    protected function logSubscriptionEvent(
        Subscription $subscription,
        string $event,
        array $data = []
    ): void {
        DB::table('subscription_events')->insert([
            'subscription_id' => $subscription->id,
            'user_id'         => $subscription->user_id,
            'event'           => $event,
            'data'            => json_encode($data),
            'created_at'      => now(),
        ]);
    }

    protected function clearUserCache(User $user): void
    {
        Cache::forget("user_subscription_{$user->id}");
        Cache::forget("user_invoices_{$user->id}");
        Cache::forget("user_credits_{$user->id}");
    }

    // ─── Public Query Methods ─────────────────────────────────────────

    /**
     * Get subscription dashboard data
     */
    public function getDashboardData(User $user): array
    {
        return Cache::remember("user_subscription_{$user->id}", 600, function () use ($user) {
            $subscription = $user->subscriptions()
                ->whereIn('status', ['active', 'trialing', 'past_due'])
                ->with('plan')
                ->latest()
                ->first();

            if (!$subscription) {
                return ['has_subscription' => false];
            }

            $usageCharges = $this->calculateUsageCharges($subscription);
            $nextInvoiceEstimate = $this->estimateNextInvoice($subscription);

            return [
                'has_subscription'     => true,
                'subscription'         => $subscription->toArray(),
                'plan'                 => $subscription->plan->toArray(),
                'usage'                => $usageCharges,
                'next_invoice'         => $nextInvoiceEstimate,
                'available_credit'     => $this->getAvailableCredit($user),
                'days_until_renewal'   => now()->diffInDays($subscription->current_period_end),
            ];
        });
    }

    /**
     * Estimate next invoice amount
     */
    protected function estimateNextInvoice(Subscription $subscription): array
    {
        $plan = $subscription->plan;
        $baseAmount = $plan->price * $subscription->quantity;

        $discount = 0;
        if ($subscription->coupon_id) {
            $coupon = Coupon::find($subscription->coupon_id);
            if ($coupon && $this->isCouponStillValid($coupon, $subscription)) {
                $discount = $this->calculateCouponDiscount($coupon, $baseAmount);
            }
        }

        $usageCharges = $this->calculateUsageCharges($subscription);

        $subtotal = $baseAmount - $discount + $usageCharges['total_overage_charge'];
        $tax = $subtotal * ($subscription->tax_rate / 100);
        $total = $subtotal + $tax;

        return [
            'base_amount'      => round($baseAmount, 2),
            'discount'         => round($discount, 2),
            'usage_charges'    => round($usageCharges['total_overage_charge'], 2),
            'subtotal'         => round($subtotal, 2),
            'tax'              => round($tax, 2),
            'estimated_total'  => round($total, 2),
            'billing_date'     => $subscription->current_period_end->format('Y-m-d'),
        ];
    }
}
