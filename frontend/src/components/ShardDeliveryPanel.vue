<template>
  <section class="publish-panel" data-testid="delivery-panel">
    <header class="panel-head">
      <div>
        <h3>分片交付处理</h3>
        <p class="panel-desc">
          未送达的交付可沿用剩余选择继续处理；已送达的交付只接受核对。同一接收凭据只结算一次，目标位置不重复写入。
        </p>
      </div>
      <button class="btn" data-testid="refresh-deliveries" type="button" @click="refreshDeliveries">
        刷新交付列表
      </button>
    </header>

    <form class="publish-form" @submit.prevent="refreshDeliveries">
      <label>
        <span>输出契约</span>
        <select v-model="contractNo">
          <option value="">全部契约</option>
          <option v-for="item in contracts" :key="item.契约编号" :value="item.契约编号">
            {{ item.契约编号 }}（{{ item.status }}）
          </option>
        </select>
      </label>
      <button class="btn" data-testid="filter-deliveries" type="submit">筛选交付</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="noticeMessage" class="notice-text" data-testid="delivery-notice">{{ noticeMessage }}</p>

    <table class="data-table" data-testid="delivery-table">
      <thead>
        <tr>
          <th>交付</th>
          <th>契约</th>
          <th>分片范围</th>
          <th>接收目标</th>
          <th>已收/总行</th>
          <th>剩余</th>
          <th>状态</th>
          <th>入口</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in deliveries"
          :key="item.交付编号"
          :data-testid="`delivery-row-${item.交付编号}`"
          :data-status="item.status"
        >
          <td>{{ item.交付编号 }}</td>
          <td>{{ item.关联契约 }}</td>
          <td>{{ item.分片范围 }}</td>
          <td>{{ item.接收目标 }}</td>
          <td>{{ item.已接收行数 }}/{{ item.总行数 }}</td>
          <td>{{ item.剩余行数 ?? remainingOf(item) }}</td>
          <td>
            <span class="badge" :data-status="item.status">{{ item.status }}</span>
          </td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`open-${item.交付编号}`"
              @click="openDetail(item.交付编号)"
            >
              打开详情
            </button>
            <button
              v-if="item.status !== '已接收'"
              class="link"
              type="button"
              :data-testid="`continue-${item.交付编号}`"
              @click="continueDelivery(item.交付编号)"
            >
              继续处理
            </button>
            <button
              v-else
              class="link"
              type="button"
              :data-testid="`verify-${item.交付编号}`"
              @click="verify(item.交付编号)"
            >
              核对
            </button>
          </td>
        </tr>
        <tr v-if="!deliveries.length">
          <td colspan="8" class="empty-state">暂无分片交付记录</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="delivery-detail" data-testid="delivery-detail">
      <header class="sheet-head">
        <h4>交付 {{ detail.交付编号 }}</h4>
        <span class="badge" :data-status="detail.status">{{ detail.status }}</span>
      </header>

      <dl class="detail-grid">
        <div><dt>关联确认单</dt><dd>{{ detail.关联确认单 }}</dd></div>
        <div><dt>分片范围</dt><dd data-testid="detail-scope">{{ detail.分片范围 }}</dd></div>
        <div><dt>接收目标</dt><dd>{{ detail.接收目标 }}</dd></div>
        <div><dt>领取人/时间</dt><dd>{{ detail.领取人 }} · {{ detail.领取时间 }}</dd></div>
        <div><dt>已接收行数</dt><dd>{{ detail.已接收行数 }} / {{ detail.总行数 }}</dd></div>
        <div><dt>剩余行数</dt><dd data-testid="detail-remaining">{{ detail.剩余行数 }}</dd></div>
      </dl>

      <div class="criteria-card" data-testid="detail-criteria">
        <h5>冻结判定口径（{{ detail.判定口径.快照时间 }}）</h5>
        <p>
          必填列 {{ detail.判定口径.必填列.join('、') || '（无）' }} ·
          输出列 {{ detail.判定口径.输出列集合.join('、') }} ·
          分片行数下限 {{ detail.判定口径.分片行数下限 }}
        </p>
        <p v-if="detail.口径回查" :data-changed="String(detail.口径回查.口径变更项.length > 0)">
          {{ detail.口径回查.说明 }}
        </p>
      </div>

      <table class="data-table" data-testid="shard-receipt-table">
        <thead>
          <tr><th>分片</th><th>分片列</th><th>总行数</th><th>已接收</th><th>回执</th><th>所属交付</th></tr>
        </thead>
        <tbody>
          <tr v-for="line in detail.分片回执" :key="line.分片编号" :data-testid="`shard-receipt-${line.分片编号}`">
            <td>{{ line.分片编号 }}</td>
            <td>{{ line.分片列.join(', ') }}</td>
            <td>{{ line.总行数 }}</td>
            <td>{{ line.已接收行数 }}</td>
            <td>{{ line.是否收齐 ? '已收齐' : '未收齐' }}</td>
            <td>{{ line.所属交付 || detail.交付编号 }}</td>
          </tr>
        </tbody>
      </table>

      <div class="remaining-box">
        <h5>可恢复的剩余选择</h5>
        <p v-if="!remainingShards.length" class="muted">没有剩余行，本次交付已全部接收。</p>
        <ul v-else>
          <li v-for="shard in remainingShards" :key="shard" :data-testid="`remaining-${shard}`">
            {{ shard }}：{{ detail.剩余选择[shard].join(', ') }}
          </li>
        </ul>
      </div>

      <form v-if="detail.status !== '已接收'" class="receive-form" @submit.prevent="receive">
        <label>
          <span>接收凭据</span>
          <input v-model="receiptToken" data-testid="receipt-token" placeholder="每次接收唯一凭据" />
        </label>
        <label>
          <span>接收目标</span>
          <input :value="detail.接收目标" readonly />
        </label>
        <label class="checkbox-line">
          <input v-model="receivedOnlyOne" type="checkbox" data-testid="interrupt-toggle" />
          <span>模拟写入中断（只接收每片首行）</span>
        </label>
        <button class="btn primary" data-testid="submit-receipt" type="submit">登记接收回执</button>
        <button class="btn" data-testid="submit-verify" type="button" :disabled="!detail.可核对" @click="verify(detail.交付编号)">
          核对已收分片
        </button>
      </form>
      <div v-else class="panel-actions">
        <button class="btn primary" data-testid="submit-verify" type="button" @click="verify(detail.交付编号)">
          核对本次交付
        </button>
      </div>

      <div v-if="detail.回执明细.length" class="receipt-log" data-testid="receipt-log">
        <h5>接收回执</h5>
        <table class="data-table">
          <thead>
            <tr><th>回执</th><th>接收凭据</th><th>本次行数</th><th>重复行</th><th>状态</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in detail.回执明细" :key="row.回执编号" :data-testid="`receipt-${row.回执编号}`">
              <td>{{ row.回执编号 }}</td>
              <td>{{ row.接收凭据 }}</td>
              <td>{{ row.接收行数 }}</td>
              <td>{{ skippedText(row.重复行) }}</td>
              <td>{{ row.status }}</td>
              <td>{{ row.接收时间 }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="ledger-card" data-testid="delivery-ledger">
        <h5>投递台账关联</h5>
        <p>投递记录 {{ detail.投递台账?.投递记录编号 }} · 状态 {{ detail.投递台账?.status }}</p>
        <p>已验证字段范围 {{ detail.投递台账?.已验证字段范围 }}</p>
        <p>分片身份 {{ detail.投递台账?.分片身份 }}</p>
        <p>接收分片行数 {{ detail.投递台账?.接收分片行数 }}</p>
        <p>接收回执 {{ detail.投递台账?.接收回执 || '（暂无）' }}</p>
        <p class="muted">{{ detail.投递台账?.投递结论 }}</p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  continueDelivery as continueDeliveryApi,
  getDelivery,
  listContracts,
  listDeliveries,
  receiveDelivery,
  verifyDelivery as verifyDeliveryApi,
  type Delivery,
  type ShardContract,
} from '@/api/shard-publish'

const props = defineProps<{ focusDelivery?: string }>()
const emit = defineEmits<{ (event: 'changed'): void }>()
const contracts = ref<ShardContract[]>([])
const contractNo = ref('')
const deliveries = ref<Delivery[]>([])
const detail = ref<Delivery | null>(null)
const receiptToken = ref('')
const receivedOnlyOne = ref(false)
const errorMessage = ref('')
const noticeMessage = ref('')

const remainingShards = computed(() =>
  detail.value ? Object.keys(detail.value.剩余选择 ?? {}) : [],
)

function remainingOf(item: Delivery): number {
  return Object.values(item.剩余选择 ?? {}).reduce((sum, rows) => sum + rows.length, 0)
}

function skippedText(value: Record<string, string[]> | string): string {
  if (!value || typeof value === 'string') return '—'
  const count = Object.values(value).reduce((sum, rows) => sum + rows.length, 0)
  return count ? String(count) : '—'
}

async function refreshDeliveries() {
  errorMessage.value = ''
  try {
    deliveries.value = await listDeliveries(contractNo.value)
    if (detail.value) {
      detail.value = await getDelivery(detail.value.交付编号)
    }
    emit('changed')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付列表读取失败'
  }
}

async function openDetail(deliveryNo: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    detail.value = await getDelivery(deliveryNo)
    receiptToken.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付详情读取失败'
  }
}

async function continueDelivery(deliveryNo: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await continueDeliveryApi(deliveryNo)
    noticeMessage.value = result.message
    detail.value = result.entry ?? (await getDelivery(deliveryNo))
    await refreshDeliveries()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '继续处理失败'
  }
}

async function receive() {
  if (!detail.value) return
  errorMessage.value = ''
  noticeMessage.value = ''
  if (!receiptToken.value.trim()) {
    errorMessage.value = '请填写接收凭据'
    return
  }
  const written: Record<string, string[]> = {}
  for (const [shardNo, rows] of Object.entries(detail.value.剩余选择 ?? {})) {
    written[shardNo] = receivedOnlyOne.value ? rows.slice(0, 1) : rows
  }
  try {
    const result = await receiveDelivery(detail.value.交付编号, {
      receipt_token: receiptToken.value.trim(),
      written,
      interrupted: receivedOnlyOne.value,
    })
    if (!result.ok) throw new Error(result.message)
    noticeMessage.value = result.message
    detail.value = result.entry ?? (await getDelivery(detail.value.交付编号))
    await refreshDeliveries()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记接收回执失败'
  }
}

async function verify(deliveryNo: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await verifyDeliveryApi(deliveryNo)
    if (!result.ok) throw new Error(result.message)
    noticeMessage.value = result.message
    detail.value = result.entry ?? (await getDelivery(deliveryNo))
    await refreshDeliveries()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核对交付失败'
  }
}

onMounted(async () => {
  try {
    contracts.value = (await listContracts()).filter((item) => item.status === '已通过')
    await refreshDeliveries()
    if (props.focusDelivery) {
      await openDetail(props.focusDelivery)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付数据读取失败'
  }
})
</script>
