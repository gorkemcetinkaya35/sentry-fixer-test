<template>
  <div class="kanban-board" @keydown="handleGlobalKeydown">
    <!-- Board Header -->
    <header class="board-header">
      <div class="board-info">
        <h1 v-if="!editingTitle" @dblclick="startEditTitle">{{ board.name }}</h1>
        <input
          v-else
          ref="titleInput"
          v-model="editableTitle"
          class="title-input"
          @blur="saveTitle"
          @keyup.enter="saveTitle"
          @keyup.escape="cancelEditTitle"
        />
        <span class="board-meta">
          {{ totalTasks }} görev · {{ completedTasks }} tamamlandı
          ({{ completionRate }}%)
        </span>
      </div>

      <div class="board-actions">
        <div class="search-box">
          <input
            v-model="searchQuery"
            type="text"
            placeholder="Görev ara..."
            @input="onSearchInput"
          />
          <span class="search-icon">🔍</span>
        </div>

        <div class="filter-group">
          <select v-model="filterPriority" @change="applyFilters">
            <option value="">Tüm Öncelikler</option>
            <option value="critical">Kritik</option>
            <option value="high">Yüksek</option>
            <option value="medium">Orta</option>
            <option value="low">Düşük</option>
          </select>

          <select v-model="filterAssignee" @change="applyFilters">
            <option value="">Tüm Kişiler</option>
            <option v-for="member in teamMembers" :key="member.id" :value="member.id">
              {{ member.name }}
            </option>
          </select>
        </div>

        <button class="btn-add-column" @click="showAddColumn = true">
          + Sütun Ekle
        </button>
      </div>
    </header>

    <!-- Columns Container -->
    <div
      class="columns-container"
      ref="columnsContainer"
      @scroll="handleColumnsScroll"
    >
      <div
        v-for="(column, colIndex) in filteredColumns"
        :key="column.id"
        class="kanban-column"
        :class="{
          'column-dragging': draggingColumn === column.id,
          'column-drop-target': dropTargetColumn === column.id,
        }"
        @dragover.prevent="onColumnDragOver($event, column.id)"
        @dragleave="onColumnDragLeave(column.id)"
        @drop="onCardDrop($event, column.id)"
      >
        <!-- Column Header -->
        <div
          class="column-header"
          draggable="true"
          @dragstart="onColumnDragStart($event, column.id)"
          @dragend="onColumnDragEnd"
        >
          <div class="column-title-row">
            <span
              class="column-color-dot"
              :style="{ backgroundColor: column.color }"
            ></span>
            <h3 v-if="editingColumnId !== column.id" @dblclick="startEditColumn(column)">
              {{ column.name }}
            </h3>
            <input
              v-else
              ref="columnTitleInput"
              v-model="editableColumnName"
              class="column-title-input"
              @blur="saveColumnName(column)"
              @keyup.enter="saveColumnName(column)"
              @keyup.escape="cancelEditColumn"
            />
            <span class="task-count">{{ getColumnTaskCount(column.id) }}</span>
          </div>

          <div class="column-actions">
            <button class="btn-icon-sm" @click="addTask(column.id)" title="Görev Ekle">+</button>
            <button class="btn-icon-sm" @click="toggleColumnMenu(column.id)" title="Menü">⋯</button>
          </div>

          <!-- Column Menu -->
          <Transition name="fade">
            <div
              v-if="activeColumnMenu === column.id"
              class="column-menu"
              @click.stop
              v-click-outside="() => activeColumnMenu = null"
            >
              <button @click="sortColumn(column.id, 'priority')">Önceliğe Göre Sırala</button>
              <button @click="sortColumn(column.id, 'date')">Tarihe Göre Sırala</button>
              <button @click="sortColumn(column.id, 'name')">İsme Göre Sırala</button>
              <hr />
              <button @click="clearColumn(column.id)" class="text-danger">
                Tümünü Temizle
              </button>
              <button
                @click="deleteColumn(column.id)"
                class="text-danger"
                v-if="columns.length > 1"
              >
                Sütunu Sil
              </button>
            </div>
          </Transition>
        </div>

        <!-- WIP Limit Warning -->
        <div
          v-if="column.wipLimit && getColumnTaskCount(column.id) > column.wipLimit"
          class="wip-warning"
        >
          ⚠ WIP limiti aşıldı ({{ getColumnTaskCount(column.id) }}/{{ column.wipLimit }})
        </div>

        <!-- Task Cards -->
        <div class="cards-container" :ref="el => cardContainers[column.id] = el">
          <TransitionGroup name="card-list" tag="div">
            <div
              v-for="task in getColumnTasks(column.id)"
              :key="task.id"
              class="task-card"
              :class="{
                'card-dragging': draggingCard === task.id,
                'card-selected': selectedTask?.id === task.id,
                'card-overdue': isOverdue(task),
              }"
              draggable="true"
              @dragstart="onCardDragStart($event, task)"
              @dragend="onCardDragEnd"
              @click="selectTask(task)"
              @dblclick="openTaskDetail(task)"
            >
              <!-- Priority & Labels -->
              <div class="card-top">
                <span class="priority-dot" :class="'priority-' + task.priority"></span>
                <div class="card-labels">
                  <span
                    v-for="label in task.labels"
                    :key="label.id"
                    class="label-badge"
                    :style="{ backgroundColor: label.color }"
                  >
                    {{ label.name }}
                  </span>
                </div>
              </div>

              <!-- Title -->
              <h4 class="card-title">{{ highlightSearch(task.title) }}</h4>

              <!-- Description preview -->
              <p v-if="task.description" class="card-description">
                {{ truncate(task.description, 80) }}
              </p>

              <!-- Subtasks Progress -->
              <div v-if="task.subtasks?.length" class="subtask-progress">
                <div class="progress-bar">
                  <div
                    class="progress-fill"
                    :style="{ width: getSubtaskProgress(task) + '%' }"
                  ></div>
                </div>
                <span class="progress-text">
                  {{ getCompletedSubtasks(task) }}/{{ task.subtasks.length }}
                </span>
              </div>

              <!-- Card Footer -->
              <div class="card-footer">
                <div class="card-assignees">
                  <img
                    v-for="assignee in task.assignees?.slice(0, 3)"
                    :key="assignee.id"
                    :src="assignee.avatar"
                    :title="assignee.name"
                    class="assignee-avatar"
                    @error="handleAvatarError($event)"
                  />
                  <span v-if="task.assignees?.length > 3" class="more-assignees">
                    +{{ task.assignees.length - 3 }}
                  </span>
                </div>

                <div class="card-meta">
                  <span v-if="task.comments_count" class="meta-item" title="Yorumlar">
                    💬 {{ task.comments_count }}
                  </span>
                  <span v-if="task.attachments_count" class="meta-item" title="Ekler">
                    📎 {{ task.attachments_count }}
                  </span>
                  <span
                    v-if="task.due_date"
                    class="meta-item due-date"
                    :class="{ 'overdue': isOverdue(task), 'due-soon': isDueSoon(task) }"
                  >
                    📅 {{ formatDate(task.due_date) }}
                  </span>
                </div>
              </div>
            </div>
          </TransitionGroup>

          <!-- Add New Task Inline -->
          <div v-if="addingTaskToColumn === column.id" class="new-task-form">
            <input
              ref="newTaskInput"
              v-model="newTaskTitle"
              placeholder="Görev başlığı..."
              @keyup.enter="submitNewTask(column.id)"
              @keyup.escape="cancelAddTask"
              @blur="cancelAddTask"
            />
          </div>
        </div>
      </div>

      <!-- Add Column Placeholder -->
      <div v-if="showAddColumn" class="add-column-form">
        <input
          ref="newColumnInput"
          v-model="newColumnName"
          placeholder="Sütun adı..."
          @keyup.enter="submitNewColumn"
          @keyup.escape="showAddColumn = false"
        />
        <div class="add-column-actions">
          <button class="btn-sm btn-primary" @click="submitNewColumn">Ekle</button>
          <button class="btn-sm btn-ghost" @click="showAddColumn = false">İptal</button>
        </div>
      </div>
    </div>

    <!-- Task Detail Drawer -->
    <Transition name="slide">
      <div v-if="taskDetailOpen" class="task-drawer" @click.self="closeTaskDetail">
        <div class="drawer-content">
          <div class="drawer-header">
            <div class="drawer-title-section">
              <span class="priority-badge" :class="'priority-' + detailTask.priority">
                {{ priorityLabels[detailTask.priority] }}
              </span>
              <input
                v-model="detailTask.title"
                class="drawer-title-input"
                @change="updateTask(detailTask)"
              />
            </div>
            <button class="btn-close-drawer" @click="closeTaskDetail">✕</button>
          </div>

          <div class="drawer-body">
            <!-- Status & Assignment -->
            <div class="detail-row">
              <label>Durum</label>
              <select v-model="detailTask.column_id" @change="moveTask(detailTask)">
                <option v-for="col in columns" :key="col.id" :value="col.id">
                  {{ col.name }}
                </option>
              </select>
            </div>

            <div class="detail-row">
              <label>Öncelik</label>
              <select v-model="detailTask.priority" @change="updateTask(detailTask)">
                <option value="critical">Kritik</option>
                <option value="high">Yüksek</option>
                <option value="medium">Orta</option>
                <option value="low">Düşük</option>
              </select>
            </div>

            <div class="detail-row">
              <label>Atanan Kişiler</label>
              <div class="assignee-selector">
                <div
                  v-for="member in teamMembers"
                  :key="member.id"
                  class="assignee-option"
                  :class="{ selected: isAssigned(detailTask, member.id) }"
                  @click="toggleAssignee(detailTask, member)"
                >
                  <img :src="member.avatar" class="option-avatar" />
                  <span>{{ member.name }}</span>
                </div>
              </div>
            </div>

            <div class="detail-row">
              <label>Bitiş Tarihi</label>
              <input
                type="date"
                v-model="detailTask.due_date"
                @change="updateTask(detailTask)"
              />
            </div>

            <!-- Description -->
            <div class="detail-row">
              <label>Açıklama</label>
              <textarea
                v-model="detailTask.description"
                rows="4"
                placeholder="Görev açıklaması..."
                @blur="updateTask(detailTask)"
              ></textarea>
            </div>

            <!-- Subtasks -->
            <div class="subtasks-section">
              <div class="section-header-row">
                <label>Alt Görevler</label>
                <button class="btn-text" @click="addSubtask">+ Ekle</button>
              </div>

              <div
                v-for="(subtask, idx) in detailTask.subtasks"
                :key="subtask.id || idx"
                class="subtask-item"
              >
                <input
                  type="checkbox"
                  v-model="subtask.completed"
                  @change="updateTask(detailTask)"
                />
                <input
                  v-model="subtask.title"
                  class="subtask-title-input"
                  @blur="updateTask(detailTask)"
                />
                <button class="btn-remove-subtask" @click="removeSubtask(idx)">✕</button>
              </div>
            </div>

            <!-- Comments -->
            <div class="comments-section">
              <label>Yorumlar ({{ detailTask.comments?.length || 0 }})</label>

              <div class="comment-input-row">
                <input
                  v-model="newComment"
                  placeholder="Yorum ekle..."
                  @keyup.enter="submitComment"
                />
                <button class="btn-sm btn-primary" @click="submitComment">Gönder</button>
              </div>

              <div class="comments-list">
                <div
                  v-for="comment in detailTask.comments"
                  :key="comment.id"
                  class="comment-item"
                >
                  <img :src="comment.author.avatar" class="comment-avatar" />
                  <div class="comment-body">
                    <div class="comment-header">
                      <strong>{{ comment.author.name }}</strong>
                      <span class="comment-time">{{ formatRelativeTime(comment.created_at) }}</span>
                    </div>
                    <p>{{ comment.text }}</p>
                  </div>
                </div>
              </div>
            </div>

            <!-- Activity Log -->
            <div class="activity-section">
              <label>Aktivite Geçmişi</label>
              <div class="activity-log">
                <div
                  v-for="(activity, idx) in detailTask.activity_log"
                  :key="idx"
                  class="activity-item"
                >
                  <span class="activity-dot"></span>
                  <span class="activity-text">{{ activity.description }}</span>
                  <span class="activity-time">{{ formatRelativeTime(activity.timestamp) }}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="drawer-footer">
            <button class="btn-danger-outline" @click="deleteTask(detailTask)">
              🗑 Görevi Sil
            </button>
            <span class="task-id">ID: {{ detailTask.id }}</span>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted, watch, nextTick, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/services/api'
import { debounce } from 'lodash-es'

export default {
  name: 'KanbanBoard',

  directives: {
    'click-outside': {
      mounted(el, binding) {
        el.__clickOutsideHandler = (event) => {
          if (!el.contains(event.target)) {
            binding.value(event)
          }
        }
        document.addEventListener('click', el.__clickOutsideHandler)
      },
      unmounted(el) {
        document.removeEventListener('click', el.__clickOutsideHandler)
      },
    },
  },

  setup() {
    const route = useRoute()
    const router = useRouter()

    // Board State
    const board = ref({ name: 'Proje Tahtası', id: null })
    const columns = ref([])
    const tasks = ref([])
    const teamMembers = ref([])

    // UI State
    const searchQuery = ref('')
    const filterPriority = ref('')
    const filterAssignee = ref('')
    const editingTitle = ref(false)
    const editableTitle = ref('')
    const editingColumnId = ref(null)
    const editableColumnName = ref('')
    const activeColumnMenu = ref(null)
    const showAddColumn = ref(false)
    const newColumnName = ref('')
    const addingTaskToColumn = ref(null)
    const newTaskTitle = ref('')
    const selectedTask = ref(null)
    const taskDetailOpen = ref(false)
    const detailTask = ref(null)
    const newComment = ref('')

    // Drag & Drop State
    const draggingCard = ref(null)
    const draggingColumn = ref(null)
    const dropTargetColumn = ref(null)
    const dragSourceColumn = ref(null)

    // Refs
    const columnsContainer = ref(null)
    const titleInput = ref(null)
    const columnTitleInput = ref(null)
    const newTaskInput = ref(null)
    const newColumnInput = ref(null)
    const cardContainers = ref({})

    // Auto-save interval
    let autoSaveInterval = null
    let unsavedChanges = ref(false)

    const priorityLabels = {
      critical: 'Kritik',
      high: 'Yüksek',
      medium: 'Orta',
      low: 'Düşük',
    }

    const priorityOrder = { critical: 0, high: 1, medium: 2, low: 3 }

    // ─── Computed ──────────────────────────────────────────────

    const filteredColumns = computed(() => {
      return columns.value.filter(col => !col._deleted)
    })

    const totalTasks = computed(() => tasks.value.filter(t => !t._deleted).length)

    const completedTasks = computed(() => {
      const doneColumns = columns.value.filter(c =>
        c.name.toLowerCase().includes('done') ||
        c.name.toLowerCase().includes('tamamlandı') ||
        c.name.toLowerCase().includes('bitti')
      )
      const doneColumnIds = doneColumns.map(c => c.id)
      return tasks.value.filter(t => doneColumnIds.includes(t.column_id) && !t._deleted).length
    })

    const completionRate = computed(() => {
      if (totalTasks.value === 0) return 0
      return Math.round((completedTasks.value / totalTasks.value) * 100)
    })

    // ─── Data Fetching ─────────────────────────────────────────

    const fetchBoard = async () => {
      const boardId = route.params.boardId
      if (!boardId) return

      try {
        const [boardRes, columnsRes, tasksRes, membersRes] = await Promise.all([
          api.get(`/boards/${boardId}`),
          api.get(`/boards/${boardId}/columns`),
          api.get(`/boards/${boardId}/tasks`),
          api.get(`/boards/${boardId}/members`),
        ])

        board.value = boardRes.data.data
        columns.value = columnsRes.data.data
        tasks.value = tasksRes.data.data
        teamMembers.value = membersRes.data.data
      } catch (error) {
        console.error('Board fetch failed:', error)
      }
    }

    // ─── Column Operations ─────────────────────────────────────

    const getColumnTasks = (columnId) => {
      let result = tasks.value.filter(t => t.column_id === columnId && !t._deleted)

      if (searchQuery.value) {
        const q = searchQuery.value.toLowerCase()
        result = result.filter(t =>
          t.title.toLowerCase().includes(q) ||
          t.description?.toLowerCase().includes(q)
        )
      }

      if (filterPriority.value) {
        result = result.filter(t => t.priority === filterPriority.value)
      }

      if (filterAssignee.value) {
        result = result.filter(t =>
          t.assignees?.some(a => a.id === filterAssignee.value)
        )
      }

      result.sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
      return result
    }

    const getColumnTaskCount = (columnId) => {
      return tasks.value.filter(t => t.column_id === columnId && !t._deleted).length
    }

    const submitNewColumn = async () => {
      if (!newColumnName.value.trim()) return

      const newColumn = {
        id: 'col-' + Date.now(),
        name: newColumnName.value.trim(),
        color: getRandomColor(),
        position: columns.value.length,
        wipLimit: null,
      }

      columns.value.push(newColumn)
      newColumnName.value = ''
      showAddColumn.value = false

      try {
        const response = await api.post(`/boards/${board.value.id}/columns`, newColumn)
        const idx = columns.value.findIndex(c => c.id === newColumn.id)
        if (idx !== -1) {
          columns.value[idx] = response.data.data
        }
      } catch (error) {
        console.error('Column creation failed:', error)
        columns.value = columns.value.filter(c => c.id !== newColumn.id)
      }
    }

    const deleteColumn = async (columnId) => {
      const columnTasks = getColumnTasks(columnId)
      if (columnTasks.length > 0) {
        if (!confirm(`Bu sütunda ${columnTasks.length} görev var. Silmek istediğinize emin misiniz?`)) {
          return
        }
      }

      const column = columns.value.find(c => c.id === columnId)
      column._deleted = true
      activeColumnMenu.value = null

      columnTasks.forEach(task => {
        task._deleted = true
      })

      try {
        await api.delete(`/boards/${board.value.id}/columns/${columnId}`)
      } catch (error) {
        column._deleted = false
        columnTasks.forEach(task => { task._deleted = false })
      }
    }

    const sortColumn = (columnId, sortBy) => {
      const columnTasks = tasks.value.filter(t => t.column_id === columnId && !t._deleted)

      columnTasks.sort((a, b) => {
        switch (sortBy) {
          case 'priority':
            return (priorityOrder[a.priority] ?? 99) - (priorityOrder[b.priority] ?? 99)
          case 'date':
            if (!a.due_date) return 1
            if (!b.due_date) return -1
            return new Date(a.due_date) - new Date(b.due_date)
          case 'name':
            return a.title.localeCompare(b.title, 'tr')
          default:
            return 0
        }
      })

      columnTasks.forEach((task, index) => {
        task.position = index
      })

      activeColumnMenu.value = null
      unsavedChanges.value = true
    }

    const clearColumn = async (columnId) => {
      if (!confirm('Bu sütundaki tüm görevler silinecek. Emin misiniz?')) return

      const columnTasks = tasks.value.filter(t => t.column_id === columnId)
      columnTasks.forEach(task => { task._deleted = true })
      activeColumnMenu.value = null

      try {
        await api.delete(`/boards/${board.value.id}/columns/${columnId}/tasks`)
      } catch (error) {
        columnTasks.forEach(task => { task._deleted = false })
      }
    }

    // ─── Task Operations ───────────────────────────────────────

    const addTask = (columnId) => {
      addingTaskToColumn.value = columnId
      nextTick(() => {
        newTaskInput.value?.focus()
      })
    }

    const submitNewTask = async (columnId) => {
      if (!newTaskTitle.value.trim()) {
        cancelAddTask()
        return
      }

      const existingTasks = getColumnTasks(columnId)
      const newTask = {
        id: 'task-' + Date.now(),
        title: newTaskTitle.value.trim(),
        description: '',
        column_id: columnId,
        priority: 'medium',
        position: existingTasks.length,
        labels: [],
        assignees: [],
        subtasks: [],
        comments: [],
        activity_log: [],
        created_at: new Date().toISOString(),
        _isNew: true,
      }

      tasks.value.push(newTask)
      newTaskTitle.value = ''
      addingTaskToColumn.value = null

      try {
        const response = await api.post(`/boards/${board.value.id}/tasks`, newTask)
        const idx = tasks.value.findIndex(t => t.id === newTask.id)
        if (idx !== -1) {
          tasks.value[idx] = { ...response.data.data, _isNew: false }
        }
      } catch (error) {
        tasks.value = tasks.value.filter(t => t.id !== newTask.id)
        console.error('Task creation failed:', error)
      }
    }

    const cancelAddTask = () => {
      addingTaskToColumn.value = null
      newTaskTitle.value = ''
    }

    const selectTask = (task) => {
      selectedTask.value = task
    }

    const openTaskDetail = async (task) => {
      try {
        const response = await api.get(`/boards/${board.value.id}/tasks/${task.id}`)
        detailTask.value = reactive({ ...response.data.data })
        taskDetailOpen.value = true
      } catch (error) {
        detailTask.value = reactive({ ...task })
        taskDetailOpen.value = true
      }
    }

    const closeTaskDetail = () => {
      taskDetailOpen.value = false
      detailTask.value = null
    }

    const updateTask = debounce(async (task) => {
      unsavedChanges.value = true

      const idx = tasks.value.findIndex(t => t.id === task.id)
      if (idx !== -1) {
        tasks.value[idx] = { ...tasks.value[idx], ...task }
      }

      try {
        await api.put(`/boards/${board.value.id}/tasks/${task.id}`, task)
        unsavedChanges.value = false
      } catch (error) {
        console.error('Task update failed:', error)
      }
    }, 500)

    const moveTask = async (task) => {
      const idx = tasks.value.findIndex(t => t.id === task.id)
      if (idx !== -1) {
        tasks.value[idx].column_id = task.column_id
      }

      try {
        await api.patch(`/boards/${board.value.id}/tasks/${task.id}/move`, {
          column_id: task.column_id,
        })
      } catch (error) {
        console.error('Task move failed:', error)
      }
    }

    const deleteTask = async (task) => {
      if (!confirm(`"${task.title}" görevini silmek istediğinize emin misiniz?`)) return

      closeTaskDetail()
      const taskIdx = tasks.value.findIndex(t => t.id === task.id)
      const removedTask = tasks.value.splice(taskIdx, 1)[0]

      try {
        await api.delete(`/boards/${board.value.id}/tasks/${task.id}`)
      } catch (error) {
        tasks.value.splice(taskIdx, 0, removedTask)
        console.error('Task deletion failed:', error)
      }
    }

    // ─── Subtasks ──────────────────────────────────────────────

    const addSubtask = () => {
      if (!detailTask.value.subtasks) {
        detailTask.value.subtasks = []
      }
      detailTask.value.subtasks.push({
        id: 'sub-' + Date.now(),
        title: '',
        completed: false,
      })
    }

    const removeSubtask = (idx) => {
      detailTask.value.subtasks.splice(idx, 1)
      updateTask(detailTask.value)
    }

    const getSubtaskProgress = (task) => {
      if (!task.subtasks || task.subtasks.length === 0) return 0
      const completed = task.subtasks.filter(s => s.completed).length
      return Math.round((completed / task.subtasks.length) * 100)
    }

    const getCompletedSubtasks = (task) => {
      return task.subtasks?.filter(s => s.completed).length || 0
    }

    // ─── Comments ──────────────────────────────────────────────

    const submitComment = async () => {
      if (!newComment.value.trim()) return

      const currentUser = teamMembers.value[0]
      const comment = {
        id: 'cmt-' + Date.now(),
        text: newComment.value.trim(),
        author: currentUser,
        created_at: new Date().toISOString(),
      }

      if (!detailTask.value.comments) {
        detailTask.value.comments = []
      }
      detailTask.value.comments.push(comment)
      newComment.value = ''

      detailTask.value.comments_count = (detailTask.value.comments_count || 0) + 1

      try {
        await api.post(
          `/boards/${board.value.id}/tasks/${detailTask.value.id}/comments`,
          { text: comment.text }
        )
      } catch (error) {
        detailTask.value.comments.pop()
        detailTask.value.comments_count--
        console.error('Comment failed:', error)
      }
    }

    // ─── Drag & Drop ───────────────────────────────────────────

    const onCardDragStart = (event, task) => {
      draggingCard.value = task.id
      dragSourceColumn.value = task.column_id
      event.dataTransfer.setData('text/plain', JSON.stringify({
        type: 'card',
        taskId: task.id,
        sourceColumn: task.column_id,
      }))
      event.dataTransfer.effectAllowed = 'move'
    }

    const onCardDragEnd = () => {
      draggingCard.value = null
      dragSourceColumn.value = null
      dropTargetColumn.value = null
    }

    const onColumnDragStart = (event, columnId) => {
      draggingColumn.value = columnId
      event.dataTransfer.setData('text/plain', JSON.stringify({
        type: 'column',
        columnId: columnId,
      }))
    }

    const onColumnDragEnd = () => {
      draggingColumn.value = null
    }

    const onColumnDragOver = (event, columnId) => {
      dropTargetColumn.value = columnId
    }

    const onColumnDragLeave = (columnId) => {
      if (dropTargetColumn.value === columnId) {
        dropTargetColumn.value = null
      }
    }

    const onCardDrop = async (event, targetColumnId) => {
      dropTargetColumn.value = null

      try {
        const data = JSON.parse(event.dataTransfer.getData('text/plain'))

        if (data.type === 'card') {
          const task = tasks.value.find(t => t.id === data.taskId)
          if (!task) return

          const oldColumnId = task.column_id
          task.column_id = targetColumnId

          const targetTasks = getColumnTasks(targetColumnId)
          task.position = targetTasks.length

          task.activity_log = task.activity_log || []
          task.activity_log.push({
            description: `Taşındı: ${getColumnName(oldColumnId)} → ${getColumnName(targetColumnId)}`,
            timestamp: new Date().toISOString(),
          })

          try {
            await api.patch(`/boards/${board.value.id}/tasks/${task.id}/move`, {
              column_id: targetColumnId,
              position: task.position,
            })
          } catch (error) {
            task.column_id = oldColumnId
            task.activity_log.pop()
          }
        }

        if (data.type === 'column') {
          const fromIdx = columns.value.findIndex(c => c.id === data.columnId)
          const toIdx = columns.value.findIndex(c => c.id === targetColumnId)

          if (fromIdx !== -1 && toIdx !== -1) {
            const [moved] = columns.value.splice(fromIdx, 1)
            columns.value.splice(toIdx, 0, moved)

            columns.value.forEach((col, idx) => { col.position = idx })
          }
        }
      } catch (e) {
        console.error('Drop error:', e)
      }
    }

    // ─── Title & Column Editing ────────────────────────────────

    const startEditTitle = () => {
      editableTitle.value = board.value.name
      editingTitle.value = true
      nextTick(() => titleInput.value?.focus())
    }

    const saveTitle = async () => {
      editingTitle.value = false
      if (editableTitle.value.trim() && editableTitle.value !== board.value.name) {
        board.value.name = editableTitle.value.trim()
        await api.patch(`/boards/${board.value.id}`, { name: board.value.name })
      }
    }

    const cancelEditTitle = () => {
      editingTitle.value = false
    }

    const startEditColumn = (column) => {
      editingColumnId.value = column.id
      editableColumnName.value = column.name
      nextTick(() => columnTitleInput.value?.focus())
    }

    const saveColumnName = async (column) => {
      editingColumnId.value = null
      if (editableColumnName.value.trim() && editableColumnName.value !== column.name) {
        column.name = editableColumnName.value.trim()
        await api.patch(
          `/boards/${board.value.id}/columns/${column.id}`,
          { name: column.name }
        )
      }
    }

    const cancelEditColumn = () => {
      editingColumnId.value = null
    }

    const toggleColumnMenu = (columnId) => {
      activeColumnMenu.value = activeColumnMenu.value === columnId ? null : columnId
    }

    // ─── Helpers ───────────────────────────────────────────────

    const getColumnName = (columnId) => {
      return columns.value.find(c => c.id === columnId)?.name || 'Bilinmeyen'
    }

    const isAssigned = (task, memberId) => {
      return task.assignees?.some(a => a.id === memberId) || false
    }

    const toggleAssignee = (task, member) => {
      if (!task.assignees) task.assignees = []
      const idx = task.assignees.findIndex(a => a.id === member.id)
      if (idx !== -1) {
        task.assignees.splice(idx, 1)
      } else {
        task.assignees.push(member)
      }
      updateTask(task)
    }

    const isOverdue = (task) => {
      if (!task.due_date) return false
      return new Date(task.due_date) < new Date()
    }

    const isDueSoon = (task) => {
      if (!task.due_date) return false
      const due = new Date(task.due_date)
      const now = new Date()
      const diff = (due - now) / (1000 * 60 * 60 * 24)
      return diff > 0 && diff <= 3
    }

    const truncate = (text, length) => {
      if (!text) return ''
      return text.length > length ? text.substring(0, length) + '...' : text
    }

    const highlightSearch = (text) => {
      if (!searchQuery.value) return text
      return text
    }

    const formatDate = (dateStr) => {
      if (!dateStr) return ''
      return new Intl.DateTimeFormat('tr-TR', {
        day: 'numeric', month: 'short',
      }).format(new Date(dateStr))
    }

    const formatRelativeTime = (dateStr) => {
      if (!dateStr) return ''
      const date = new Date(dateStr)
      const now = new Date()
      const diffMs = now - date
      const diffMins = Math.floor(diffMs / 60000)
      const diffHours = Math.floor(diffMins / 60)
      const diffDays = Math.floor(diffHours / 24)

      if (diffMins < 1) return 'az önce'
      if (diffMins < 60) return `${diffMins} dk önce`
      if (diffHours < 24) return `${diffHours} saat önce`
      if (diffDays < 7) return `${diffDays} gün önce`
      return formatDate(dateStr)
    }

    const getRandomColor = () => {
      const colors = ['#4F46E5', '#10B981', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#06B6D4']
      return colors[Math.floor(Math.random() * colors.length)]
    }

    const handleAvatarError = (event) => {
      event.target.src = '/images/default-avatar.png'
    }

    const applyFilters = () => { /* filters are reactive */ }

    const onSearchInput = debounce(() => { /* search is reactive */ }, 200)

    const handleColumnsScroll = () => { /* placeholder for infinite scroll */ }

    // ─── Keyboard Shortcuts ────────────────────────────────────

    const handleGlobalKeydown = (event) => {
      if (event.key === 'Escape') {
        if (taskDetailOpen.value) closeTaskDetail()
        if (activeColumnMenu.value) activeColumnMenu.value = null
      }

      if (event.ctrlKey && event.key === 'n') {
        event.preventDefault()
        const firstCol = columns.value[0]
        if (firstCol) addTask(firstCol.id)
      }

      if (event.ctrlKey && event.key === 's') {
        event.preventDefault()
        saveBoard()
      }
    }

    const saveBoard = async () => {
      if (!unsavedChanges.value) return

      try {
        await api.put(`/boards/${board.value.id}/bulk-update`, {
          columns: columns.value,
          tasks: tasks.value.filter(t => !t._deleted),
        })
        unsavedChanges.value = false
      } catch (error) {
        console.error('Board save failed:', error)
      }
    }

    // ─── Lifecycle ─────────────────────────────────────────────

    onMounted(() => {
      fetchBoard()

      autoSaveInterval = setInterval(() => {
        if (unsavedChanges.value) {
          saveBoard()
        }
      }, 30000)
    })

    onUnmounted(() => {
      if (autoSaveInterval) {
        clearInterval(autoSaveInterval)
      }
    })

    watch(() => route.params.boardId, (newId, oldId) => {
      if (newId !== oldId) {
        fetchBoard()
      }
    })

    return {
      board,
      columns,
      tasks,
      teamMembers,
      searchQuery,
      filterPriority,
      filterAssignee,
      editingTitle,
      editableTitle,
      editingColumnId,
      editableColumnName,
      activeColumnMenu,
      showAddColumn,
      newColumnName,
      addingTaskToColumn,
      newTaskTitle,
      selectedTask,
      taskDetailOpen,
      detailTask,
      newComment,
      draggingCard,
      draggingColumn,
      dropTargetColumn,
      columnsContainer,
      titleInput,
      columnTitleInput,
      newTaskInput,
      newColumnInput,
      cardContainers,
      priorityLabels,
      filteredColumns,
      totalTasks,
      completedTasks,
      completionRate,
      getColumnTasks,
      getColumnTaskCount,
      submitNewColumn,
      deleteColumn,
      sortColumn,
      clearColumn,
      addTask,
      submitNewTask,
      cancelAddTask,
      selectTask,
      openTaskDetail,
      closeTaskDetail,
      updateTask,
      moveTask,
      deleteTask,
      addSubtask,
      removeSubtask,
      getSubtaskProgress,
      getCompletedSubtasks,
      submitComment,
      onCardDragStart,
      onCardDragEnd,
      onColumnDragStart,
      onColumnDragEnd,
      onColumnDragOver,
      onColumnDragLeave,
      onCardDrop,
      startEditTitle,
      saveTitle,
      cancelEditTitle,
      startEditColumn,
      saveColumnName,
      cancelEditColumn,
      toggleColumnMenu,
      isAssigned,
      toggleAssignee,
      isOverdue,
      isDueSoon,
      truncate,
      highlightSearch,
      formatDate,
      formatRelativeTime,
      handleAvatarError,
      applyFilters,
      onSearchInput,
      handleColumnsScroll,
      handleGlobalKeydown,
    }
  },
}
</script>

<style scoped>
.kanban-board {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #f0f2f5;
  overflow: hidden;
}

.board-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: white;
  border-bottom: 1px solid #e5e7eb;
  flex-shrink: 0;
}

.board-info h1 {
  font-size: 1.5rem;
  font-weight: 700;
  color: #1f2937;
  cursor: pointer;
}

.board-meta {
  font-size: 0.8rem;
  color: #6b7280;
  margin-top: 2px;
  display: block;
}

.title-input {
  font-size: 1.5rem;
  font-weight: 700;
  border: 2px solid #4f46e5;
  border-radius: 6px;
  padding: 4px 8px;
  outline: none;
}

.board-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.search-box {
  position: relative;
}

.search-box input {
  padding: 8px 12px 8px 32px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  width: 220px;
  font-size: 0.85rem;
}

.search-icon {
  position: absolute;
  left: 10px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 0.85rem;
}

.filter-group {
  display: flex;
  gap: 8px;
}

.filter-group select {
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 0.85rem;
}

.btn-add-column {
  padding: 8px 16px;
  background: #4f46e5;
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 0.85rem;
  cursor: pointer;
  white-space: nowrap;
}

.btn-add-column:hover {
  background: #4338ca;
}

/* Columns Container */
.columns-container {
  display: flex;
  gap: 16px;
  padding: 20px 24px;
  overflow-x: auto;
  flex: 1;
  align-items: flex-start;
}

.kanban-column {
  min-width: 300px;
  max-width: 340px;
  background: #f9fafb;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - 140px);
  flex-shrink: 0;
}

.column-dragging { opacity: 0.5; }
.column-drop-target { outline: 2px dashed #4f46e5; }

.column-header {
  padding: 12px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: relative;
  cursor: grab;
  flex-wrap: wrap;
}

.column-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.column-color-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.column-header h3 {
  font-size: 0.9rem;
  font-weight: 600;
  color: #374151;
  cursor: text;
}

.column-title-input {
  font-size: 0.9rem;
  font-weight: 600;
  border: 1px solid #4f46e5;
  border-radius: 4px;
  padding: 2px 6px;
  width: 120px;
}

.task-count {
  font-size: 0.75rem;
  background: #e5e7eb;
  color: #374151;
  padding: 2px 8px;
  border-radius: 10px;
}

.column-actions {
  display: flex;
  gap: 4px;
}

.btn-icon-sm {
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: 6px;
  font-size: 1rem;
  display: flex;
  align-items: center;
  justify-content: center;
}

.btn-icon-sm:hover { background: #e5e7eb; }

.column-menu {
  position: absolute;
  top: 100%;
  right: 0;
  background: white;
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  z-index: 100;
  min-width: 180px;
  overflow: hidden;
}

.column-menu button {
  display: block;
  width: 100%;
  text-align: left;
  padding: 10px 16px;
  border: none;
  background: none;
  cursor: pointer;
  font-size: 0.85rem;
}

.column-menu button:hover { background: #f3f4f6; }
.column-menu hr { margin: 4px 0; border: none; border-top: 1px solid #e5e7eb; }
.text-danger { color: #ef4444 !important; }

.wip-warning {
  background: #fef3c7;
  color: #92400e;
  font-size: 0.75rem;
  padding: 6px 16px;
  text-align: center;
  font-weight: 500;
}

.cards-container {
  padding: 8px 12px;
  overflow-y: auto;
  flex: 1;
}

/* Task Cards */
.task-card {
  background: white;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 8px;
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  border: 1px solid transparent;
  transition: all 0.15s;
}

.task-card:hover {
  box-shadow: 0 2px 8px rgba(0,0,0,0.1);
  border-color: #e5e7eb;
}

.card-dragging { opacity: 0.4; }
.card-selected { border-color: #4f46e5; }
.card-overdue { border-left: 3px solid #ef4444; }

.card-top {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.priority-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.priority-critical { background: #ef4444; }
.priority-high { background: #f59e0b; }
.priority-medium { background: #3b82f6; }
.priority-low { background: #9ca3af; }

.card-labels { display: flex; gap: 4px; flex-wrap: wrap; }

.label-badge {
  font-size: 0.65rem;
  padding: 2px 6px;
  border-radius: 4px;
  color: white;
  font-weight: 500;
}

.card-title {
  font-size: 0.875rem;
  font-weight: 500;
  color: #1f2937;
  margin-bottom: 4px;
  line-height: 1.3;
}

.card-description {
  font-size: 0.75rem;
  color: #6b7280;
  margin-bottom: 8px;
  line-height: 1.4;
}

.subtask-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.progress-bar {
  flex: 1;
  height: 4px;
  background: #e5e7eb;
  border-radius: 2px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: #10b981;
  border-radius: 2px;
  transition: width 0.3s;
}

.progress-text {
  font-size: 0.7rem;
  color: #6b7280;
  white-space: nowrap;
}

.card-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-assignees {
  display: flex;
  align-items: center;
}

.assignee-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 2px solid white;
  margin-left: -6px;
  object-fit: cover;
}

.assignee-avatar:first-child { margin-left: 0; }

.more-assignees {
  font-size: 0.7rem;
  color: #6b7280;
  margin-left: 4px;
}

.card-meta {
  display: flex;
  gap: 8px;
  font-size: 0.7rem;
  color: #9ca3af;
}

.due-date.overdue { color: #ef4444; font-weight: 600; }
.due-date.due-soon { color: #f59e0b; font-weight: 600; }

/* New Task Form */
.new-task-form input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 0.85rem;
}

/* Add Column Form */
.add-column-form {
  min-width: 280px;
  background: #f9fafb;
  border-radius: 12px;
  padding: 16px;
  flex-shrink: 0;
}

.add-column-form input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 0.85rem;
  margin-bottom: 12px;
}

.add-column-actions {
  display: flex;
  gap: 8px;
}

.btn-sm { padding: 6px 14px; border-radius: 6px; font-size: 0.8rem; cursor: pointer; }
.btn-primary { background: #4f46e5; color: white; border: none; }
.btn-ghost { background: transparent; border: 1px solid #d1d5db; }

/* Task Drawer */
.task-drawer {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.4);
  display: flex;
  justify-content: flex-end;
  z-index: 200;
}

.drawer-content {
  width: 520px;
  max-width: 90vw;
  background: white;
  height: 100%;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.drawer-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 20px 24px;
  border-bottom: 1px solid #e5e7eb;
}

.drawer-title-input {
  display: block;
  width: 100%;
  font-size: 1.1rem;
  font-weight: 600;
  border: none;
  border-bottom: 2px solid transparent;
  padding: 4px 0;
  outline: none;
  margin-top: 8px;
}

.drawer-title-input:focus {
  border-bottom-color: #4f46e5;
}

.btn-close-drawer {
  width: 32px;
  height: 32px;
  border: none;
  background: #f3f4f6;
  border-radius: 8px;
  cursor: pointer;
  flex-shrink: 0;
}

.priority-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 0.7rem;
  font-weight: 600;
  color: white;
}

.priority-badge.priority-critical { background: #ef4444; }
.priority-badge.priority-high { background: #f59e0b; }
.priority-badge.priority-medium { background: #3b82f6; }
.priority-badge.priority-low { background: #9ca3af; }

.drawer-body {
  padding: 24px;
  flex: 1;
  overflow-y: auto;
}

.detail-row {
  margin-bottom: 20px;
}

.detail-row label {
  display: block;
  font-size: 0.75rem;
  font-weight: 600;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}

.detail-row select,
.detail-row input[type="date"],
.detail-row textarea {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 0.85rem;
}

.detail-row textarea { resize: vertical; }

.assignee-selector {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.assignee-option {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  cursor: pointer;
  font-size: 0.8rem;
  transition: all 0.15s;
}

.assignee-option.selected {
  border-color: #4f46e5;
  background: #eef2ff;
}

.option-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  object-fit: cover;
}

/* Subtasks */
.subtasks-section { margin-bottom: 24px; }
.section-header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.btn-text {
  background: none;
  border: none;
  color: #4f46e5;
  cursor: pointer;
  font-size: 0.8rem;
  font-weight: 500;
}

.subtask-item {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.subtask-title-input {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  font-size: 0.85rem;
}

.btn-remove-subtask {
  border: none;
  background: transparent;
  color: #9ca3af;
  cursor: pointer;
  font-size: 0.85rem;
}

.btn-remove-subtask:hover { color: #ef4444; }

/* Comments */
.comments-section { margin-bottom: 24px; }

.comment-input-row {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.comment-input-row input {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 0.85rem;
}

.comment-item {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}

.comment-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
}

.comment-body { flex: 1; }

.comment-header {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 4px;
}

.comment-header strong { font-size: 0.85rem; }

.comment-time {
  font-size: 0.7rem;
  color: #9ca3af;
}

.comment-body p {
  font-size: 0.85rem;
  color: #374151;
  line-height: 1.4;
}

/* Activity Log */
.activity-section { margin-bottom: 24px; }

.activity-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 0;
  font-size: 0.8rem;
}

.activity-dot {
  width: 8px;
  height: 8px;
  background: #d1d5db;
  border-radius: 50%;
  margin-top: 4px;
  flex-shrink: 0;
}

.activity-text { flex: 1; color: #374151; }
.activity-time { color: #9ca3af; font-size: 0.7rem; white-space: nowrap; }

.drawer-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  border-top: 1px solid #e5e7eb;
}

.btn-danger-outline {
  padding: 6px 14px;
  border: 1px solid #ef4444;
  color: #ef4444;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.85rem;
}

.btn-danger-outline:hover {
  background: #fef2f2;
}

.task-id {
  font-size: 0.75rem;
  color: #9ca3af;
}

/* Transitions */
.slide-enter-active { transition: transform 0.3s ease; }
.slide-leave-active { transition: transform 0.25s ease; }
.slide-enter-from { transform: translateX(100%); }
.slide-leave-to { transform: translateX(100%); }

.fade-enter-active, .fade-leave-active { transition: opacity 0.15s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

.card-list-move { transition: transform 0.3s; }
.card-list-enter-active { transition: all 0.3s ease; }
.card-list-leave-active { transition: all 0.2s ease; position: absolute; }
.card-list-enter-from { opacity: 0; transform: translateY(-10px); }
.card-list-leave-to { opacity: 0; }

/* Responsive */
@media (max-width: 768px) {
  .board-header { flex-direction: column; gap: 12px; padding: 12px 16px; }
  .board-actions { flex-wrap: wrap; }
  .kanban-column { min-width: 260px; }
  .drawer-content { width: 100vw; }
}
</style>
