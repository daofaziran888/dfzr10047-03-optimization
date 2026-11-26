<template>
  <section class="publish-panel" data-testid="shard-publish-panel">
    <header class="panel-head">
      <div>
        <h3>分片选择与确认单</h3>
        <p class="panel-desc">
          按接收目标逐片核对必填列，选中已具备单独交付资格的分片提交；缺列分片不会并入本次交付。
        </p>
      </div>
      <button class="btn" data-testid="refresh-receipt" type="button" @click="refreshReceipt()">
        刷新分片回执
      </button>
    </header>

    <form class="publish-form" @submit.prevent="refreshReceipt()">
      <label>
        <span>输出契约</span>
        <select v-model="contractNo" data-testid="contract-select" @change="onContractChange">
          <option v-for="item in contracts" :key="item.契约编号" :value="item.契约编号">
            {{ item.契约编号 }}（{{ item.status }}）
          </option>
        </select>
      </label>
      <label>
        <span>接收目标</span>
        <select v-model="target" data-testid="target-select" @change="onTargetChange">
          <option value="">全部接收目标</option>
          <option v-for="item in targets" :key="item.接收目标" :value="item.接收目标">
            {{ item.接收目标 }}
          </option>
        </select>
      </label>
      <button class="btn primary" data-testid="load-receipt" type="submit">读取校验回执</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="noticeMessage" class="notice-text" data-testid="publish-notice">{{ noticeMessage }}</p>

    <div v-if="criteria" class="criteria-bar" data-testid="criteria-bar">
      <span>判定口径：必填列 {{ criteria.必填列.join('、') || '（无）' }}</span>
      <span>输出列 {{ criteria.输出列集合.join('、') }}</span>
      <span>分片行数下限 {{ criteria.分片行数下限 }}</span>
      <span>锁定时间 {{ criteria.快照时间 }}</span>
    </div>

    <div v-for="group in visibleTargets" :key="group.接收目标" class="target-group">
      <h4 class="target-title" :data-testid="`target-${group.接收目标}`">
        {{ group.接收目标 }}
        <small>
          可发 {{ group.可发分片.length }} 片 · 缺列 {{ group.缺列分片.length }} 片
        </small>
      </h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>选择</th>
            <th>分片</th>
            <th>分片列</th>
            <th>行数</th>
            <th>判定</th>
            <th>补值</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="line in group.分片"
            :key="line.分片编号"
            :data-testid="`shard-${line.分片编号}`"
            :data-publishable="String(line.可发)"
          >
            <td>
              <input
                type="checkbox"
                :checked="selected.includes(line.分片编号)"
                :disabled="line.分片状态 !== '待发布'"
                :data-testid="`select-${line.分片编号}`"
                @change="toggleSelect(line.分片编号)"
              />
            </td>
            <td>
              <strong>{{ line.分片编号 }}</strong>
              <small class="shard-state">{{ line.分片状态 }}</small>
            </td>
            <td>{{ line.分片列.join(', ') }}</td>
            <td>{{ line.行数 }}</td>
            <td>
              <span :class="line.可发 ? 'verdict-ok' : 'verdict-bad'" :data-testid="`verdict-${line.分片编号}`">
                {{ line.判定结论 || (line.可发 ? '可发' : '缺列') }}
              </span>
              <small class="verdict-detail">{{ line.结论 }}</small>
              <small
                v-if="line.所属交付"
                class="shard-state"
                :data-testid="`owner-${line.分片编号}`"
              >
                已领取：{{ line.所属交付 }}
              </small>
            </td>
            <td>
              <template v-if="line.分片状态 === '待发布' && !line.可发">
                <input
                  v-model="supplementInput[line.分片编号]"
                  class="supplement-input"
                  :data-testid="`supplement-input-${line.分片编号}`"
                  placeholder="补齐列，逗号分隔"
                />
                <button
                  class="link"
                  type="button"
                  :data-testid="`supplement-${line.分片编号}`"
                  @click="supplement(line)"
                >
                  补齐
                </button>
              </template>
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel-actions">
      <button class="btn primary" data-testid="build-sheet" type="button" :disabled="!selected.length" @click="buildSheet">
        编制确认单（{{ selected.length }} 片）
      </button>
      <span class="muted">同一张确认单会逐片核对哪些可发、哪些缺列。</span>
    </div>

    <div v-if="sheet" class="sheet-card" data-testid="confirm-sheet">
      <header class="sheet-head">
        <h4>确认单 {{ sheet.确认单编号 }}</h4>
        <span class="badge" :data-status="sheet.status">{{ sheet.status }}</span>
      </header>
      <p class="sheet-meta">
        契约 {{ sheet.关联契约 }} · 目标 {{ sheet.接收目标 }} · 操作人 {{ sheet.操作人 }}
        · 可发 {{ sheet.可发分片 || '无' }} · 缺列 {{ sheet.缺列分片 || '无' }}
      </p>
      <table class="data-table">
        <thead>
          <tr><th>分片</th><th>分片列</th><th>必填列</th><th>行数</th><th>判定</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="line in sheet.行明细"
            :key="line.分片编号"
            :data-testid="`sheet-line-${line.分片编号}`"
            :data-decision="line.判定结论 || (line.可发 ? '可发' : '缺列')"
          >
            <td>{{ line.分片编号 }}</td>
            <td>{{ line.分片列.join(', ') }}</td>
            <td>{{ line.必填列.join(', ') }}</td>
            <td>{{ line.行数 }}</td>
            <td>
              <span :class="line.可发 ? 'verdict-ok' : 'verdict-bad'">
                {{ line.判定结论 || (line.可发 ? '可发' : '缺列') }}
              </span>
            </td>
            <td class="verdict-detail">{{ line.结论 }}</td>
          </tr>
        </tbody>
      </table>
      <div class="panel-actions">
        <button
          class="btn primary"
          data-testid="submit-sheet"
          type="button"
          :disabled="sheet.status !== '已编制'"
          @click="submitSheet"
        >
          领取已合格分片
        </button>
        <span v-if="sheet.未并入分片" class="muted" data-testid="sheet-excluded">
          未并入本次交付：{{ sheet.未并入分片 }}
        </span>
      </div>
    </div>

    <div v-if="delivery" class="delivery-summary" data-testid="delivery-summary">
      <h4>本次交付 {{ delivery.交付编号 }}</h4>
      <p>
        分片 {{ delivery.分片范围 }} · {{ delivery.总行数 }} 行 → {{ delivery.接收目标 }}
      </p>
      <p v-if="delivery.口径变更提示" class="warn-text">{{ delivery.口径变更提示 }}</p>
      <p class="muted">
        台账 {{ delivery.投递台账?.投递记录编号 }} 关联投递；接收与核对请到「结果投递」页继续处理。
      </p>
      <button class="link" type="button" data-testid="goto-delivery" @click="emit('open-delivery', delivery.交付编号)">
        查看本次交付详情
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  buildConfirmSheet,
  fetchShardReceipt,
  listContracts,
  submitConfirmSheet,
  supplementShard,
  type ConfirmSheet,
  type Criteria,
  type Delivery,
  type ShardContract,
  type ShardLine,
  type ShardReceipt,
  type TargetReceipt,
} from '@/api/shard-publish'

const emit = defineEmits<{ (event: 'open-delivery', deliveryNo: string): void }>()

const contracts = ref<ShardContract[]>([])
const contractNo = ref('')
const target = ref('')
const receipt = ref<ShardReceipt | null>(null)
const criteria = ref<Criteria | null>(null)
const selected = ref<string[]>([])
const supplementInput = ref<Record<string, string>>({})
const sheet = ref<ConfirmSheet | null>(null)
const delivery = ref<Delivery | null>(null)
const errorMessage = ref('')
const noticeMessage = ref('')

const targets = computed<TargetReceipt[]>(() => receipt.value?.接收目标 ?? [])
const visibleTargets = computed<TargetReceipt[]>(() =>
  target.value ? targets.value.filter((item) => item.接收目标 === target.value) : targets.value,
)
const operator = 'publisher-01'

function toggleSelect(shardNo: string) {
  if (selected.value.includes(shardNo)) {
    selected.value = selected.value.filter((item) => item !== shardNo)
  } else {
    selected.value = [...selected.value, shardNo]
  }
}

async function loadContracts() {
  contracts.value = (await listContracts()).filter((item) => item.status === '已通过')
  if (!contractNo.value && contracts.value.length) {
    contractNo.value = contracts.value[0].契约编号
  }
}

async function refreshReceipt(silent = false) {
  errorMessage.value = ''
  if (!silent) noticeMessage.value = ''
  if (!contractNo.value) return
  try {
    const result = await fetchShardReceipt(contractNo.value, target.value)
    if (!result.ok) throw new Error(result.message)
    receipt.value = result.entry ?? null
    criteria.value = receipt.value?.判定口径 ?? null
    if (!target.value && targets.value.length === 1) {
      target.value = targets.value[0].接收目标
    }
    selected.value = []
    sheet.value = null
    if (silent) return
    const lines = visibleTargets.value.flatMap((group) => group.分片)
    noticeMessage.value = `已读取 ${lines.length} 个分片：可发 ${
      lines.filter((line) => line.可发).length
    } 片，缺列 ${lines.filter((line) => !line.可发).length} 片`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分片回执读取失败'
  }
}

async function onContractChange() {
  target.value = ''
  await refreshReceipt()
}

async function onTargetChange() {
  await refreshReceipt()
}

async function supplement(line: ShardLine) {
  errorMessage.value = ''
  noticeMessage.value = ''
  const raw = supplementInput.value[line.分片编号] ?? ''
  const columns = raw.split(',').map((item) => item.trim()).filter(Boolean)
  if (!columns.length) {
    errorMessage.value = '请先填写要补齐的列名'
    return
  }
  try {
    const result = await supplementShard(line.分片编号, columns)
    if (!result.ok) throw new Error(result.message)
    noticeMessage.value = result.message
    supplementInput.value[line.分片编号] = ''
    await refreshReceipt()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补齐分片字段失败'
  }
}

async function buildSheet() {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await buildConfirmSheet({
      contract_no: contractNo.value,
      target: target.value,
      shard_numbers: selected.value,
      operator,
    })
    if (!result.ok) throw new Error(result.message)
    sheet.value = result.entry ?? null
    delivery.value = null
    noticeMessage.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '编制确认单失败'
  }
}

async function submitSheet() {
  if (!sheet.value) return
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await submitConfirmSheet(sheet.value.确认单编号, { operator })
    if (!result.ok) throw new Error(result.message)
    delivery.value = result.entry ?? null
    sheet.value = null
    // 静默刷新分片状态，保留「已领取/未并入」的提示
    await refreshReceipt(true)
    noticeMessage.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交确认单失败'
  }
}

onMounted(async () => {
  try {
    await loadContracts()
    await refreshReceipt()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分片发布数据读取失败'
  }
})
</script>
