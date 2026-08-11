"""
sentry-fixer'in ürettiği fix'leri doğrulayan testler.

İki işi var:
  1. Smoke — modülün ana sınıfları kurulabiliyor mu? Bir LLM düzeltme yaparken
     alakasız kod silerse burada patlar. (Gerçekten oldu: bir fix denemesinde
     SlidingWindowRateLimiter sınıfının tamamı uçmuştu.)
  2. Regresyon — decrypt_message kırpılmış girdide düzgün hata veriyor mu?
"""
import base64

import pytest

from messaging_engine import (
    MessageCache,
    MessageEncryption,
    MessagingEngine,
    NotificationManager,
    SlidingWindowRateLimiter,
)


# --- smoke: collateral damage yakalayıcı ---

def test_engine_constructs():
    """MessagingEngine() tüm yardımcı sınıfları kuruyor — biri silinirse patlar."""
    assert MessagingEngine() is not None


def test_helper_classes_construct():
    assert SlidingWindowRateLimiter(max_requests=5, window_seconds=60) is not None
    assert MessageCache(max_size=10) is not None
    assert MessageEncryption() is not None
    assert NotificationManager() is not None


def test_rate_limiter_enforces_limit():
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
    assert limiter.is_allowed("u1") is True
    assert limiter.is_allowed("u1") is True
    assert limiter.is_allowed("u1") is False


def test_cache_hit_rate_on_empty_cache():
    assert MessageCache(max_size=10).hit_rate == 0.0


# --- şifreleme ---

def test_encrypt_decrypt_roundtrip():
    crypto = MessageEncryption()
    encrypted = crypto.encrypt_message("merhaba dünya", "conversation-1")
    assert crypto.decrypt_message(encrypted, "conversation-1") == "merhaba dünya"


def test_decrypt_rejects_tampered_message():
    crypto = MessageEncryption()
    encrypted = crypto.encrypt_message("merhaba", "conversation-1")
    raw = bytearray(base64.b64decode(encrypted))
    raw[-1] ^= 0xFF  # son byte'ı boz
    tampered = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(ValueError):
        crypto.decrypt_message(tampered, "conversation-1")


# --- arama / alaka puanı ---

def test_calculate_relevance_scores_matching_content():
    """
    REGRESYON — `_calculate_relevance` her çağrıda patlıyordu.

    length_penalty hesabında `max()` tek argümanla çağrılıyor
    (`max(len(content),)`), oysa `max` tek argüman alınca iterable bekler:
    `TypeError: 'int' object is not iterable`. Boş içerikte değil, arama
    yapılan **her** mesajda tetikleniyor.
    """
    engine = MessagingEngine()
    score = engine._calculate_relevance("merhaba dünya", "dünya")

    assert isinstance(score, float)
    assert score > 0


def test_calculate_relevance_handles_empty_content():
    """Boş içerik sıfıra bölmemeli, 0.0 dönmeli."""
    assert MessagingEngine()._calculate_relevance("", "sorgu") == 0.0


def test_decrypt_truncated_message_raises_value_error():
    """
    REGRESYON — kırpılmış/bozuk mesaj.

    decrypt_message gelen veriyi doğrulamadan struct.unpack('>I', decrypted[:4])
    çağırıyor. 16 byte'tan kısa girdide struct.error fırlıyor; oysa hemen altında
    bütünlük hatası için ValueError yolu var (struct.error ValueError'dan
    türemiyor, çağıran taraf yakalayamıyor).

    Bu test fix uygulanana kadar KIRMIZI kalır.
    """
    crypto = MessageEncryption()
    truncated = base64.b64encode(b"kirpik").decode("ascii")

    with pytest.raises(ValueError):
        crypto.decrypt_message(truncated, "conversation-42")
