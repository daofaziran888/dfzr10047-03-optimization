/** 待发布分片发布链路的接口：分片校验回执、确认单、领取、接收回执与核对。 */
import { request } from './client'

export type ShardLine = {
  分片编号: string
  关联契约: string
  接收目标: string
  分片列: string[]
  行数: number
  行标识: string[]
  必填列: string[]
  缺失必填列: string[]
  未约定列: string[]
  可发: boolean
  结论: string
  分片状态: string
  所属交付?: string
  所属确认单?: string
  判定结论?: string
  校验时间: string
}

export type TargetReceipt = {
  接收目标: string
  分片: ShardLine[]
  可发分片: string[]
  缺列分片: string[]
}

export type Criteria = {
  输出列集合: string[]
  列顺序约定: string[]
  必填列: string[]
  分片行数下限: number
  整批行数下限: number
  契约编号: string
  契约状态: string
  契约校验时间: string
  快照时间: string
}

export type ShardReceipt = {
  关联契约: string
  判定口径: Criteria
  接收目标: TargetReceipt[]
  校验时间: string
}

export type ConfirmSheet = {
  id: number
  status: string
  确认单编号: string
  关联契约: string
  接收目标: string
  操作人: string
  编制时间: string
  提交时间: string
  分片范围: string
  可发分片: string
  缺列分片: string
  已提交分片?: string
  未并入分片?: string
  判定口径: Criteria
  行明细: ShardLine[]
  所属交付: string
  交付列表?: { 交付编号: string; status: string; 分片范围: string; 已接收行数: string }[]
}

export type ShardReceiptLine = {
  分片编号: string
  总行数: number
  已接收行数: number
  是否收齐: boolean
  分片列: string[]
  所属交付?: string
  所属确认单?: string
}

export type ReceiptRecord = {
  回执编号: string
  接收凭据: string
  接收时间: string
  接收行数: string
  status: string
  接收行: Record<string, string[]> | string
  重复行: Record<string, string[]> | string
  幂等重放: boolean
}

export type DeliveryLedger = {
  投递记录编号: string
  关联契约: string
  目标位置: string
  投递行数: string
  投递结论: string
  投递时间: string
  分片身份: string
  已验证字段范围: string
  接收回执: string
  已接收行数: string
  接收分片行数: string
  status?: string
}

export type Delivery = {
  id: number
  status: string
  交付编号: string
  关联确认单: string
  关联契约: string
  接收目标: string
  领取人: string
  领取时间: string
  分片范围: string
  冻结范围: { 分片编号: string; 分片列: string[]; 行数: number; 行标识: string[]; 接收目标: string }[]
  冻结判定: { 分片编号: string; 分片列: string[]; 必填列: string[]; 行数: number; 结论: string; 校验时间: string }[]
  判定口径: Criteria
  口径变更提示: string
  总行数: string
  已接收行数: string
  剩余选择: Record<string, string[]>
  分片回执: ShardReceiptLine[]
  回执明细: ReceiptRecord[]
  剩余行数: number
  已收齐分片: string[]
  未收齐分片: string[]
  可继续处理: boolean
  可核对: boolean
  口径回查: {
    冻结分片: string[]
    冻结行数: number
    冻结必填列: string[]
    冻结分片行数下限: number
    冻结时间: string
    当前必填列: string[]
    当前分片行数下限: number
    口径变更项: string[]
    沿用冻结口径: boolean
    说明: string
  }
  投递台账: DeliveryLedger
}

export type ActionResult<T = unknown> = {
  ok: boolean
  message: string
  entry?: T | null
}

async function postJson<T>(path: string, body?: unknown): Promise<ActionResult<T>> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  return (await response.json()) as ActionResult<T>
}

export async function listContracts(): Promise<ShardContract[]> {
  const response = await request('/api/output_contract?size=200')
  if (!response.ok) throw new Error('输出契约读取失败')
  const payload = (await response.json()) as { items: ShardContract[] }
  return payload.items
}

export type ShardContract = {
  id: number
  status: string
  契约编号: string
  关联批次: string
  输出列集合: string
  列顺序约定: string
  必填列: string
  行数下限: string
  分片行数下限?: string
}

export async function listShards(contract: string): Promise<Record<string, unknown>[]> {
  const response = await request(`/api/shard-publish/shards?contract=${encodeURIComponent(contract)}`)
  if (!response.ok) throw new Error('待发布分片读取失败')
  const payload = (await response.json()) as { items: Record<string, unknown>[] }
  return payload.items
}

export function fetchShardReceipt(
  contract: string,
  target = '',
): Promise<ActionResult<ShardReceipt>> {
  const query = new URLSearchParams({ contract, target }).toString()
  return fetchAction<ShardReceipt>(`/api/shard-publish/receipt?${query}`)
}

async function fetchAction<T>(path: string): Promise<ActionResult<T>> {
  const response = await request(path)
  return (await response.json()) as ActionResult<T>
}

export function supplementShard(
  shardNo: string,
  addColumns: string[],
): Promise<ActionResult> {
  return postJson('/api/shard-publish/supplement', {
    shard_no: shardNo,
    add_columns: addColumns,
  })
}

export function buildConfirmSheet(payload: {
  contract_no: string
  target: string
  shard_numbers: string[]
  operator: string
}): Promise<ActionResult<ConfirmSheet>> {
  return postJson('/api/shard-publish/sheets', payload)
}

export function submitConfirmSheet(
  sheetNo: string,
  payload: { operator?: string; shard_numbers?: string[] | null } = {},
): Promise<ActionResult<Delivery>> {
  return postJson(`/api/shard-publish/sheets/${encodeURIComponent(sheetNo)}/submit`, payload)
}

export async function listSheets(contract = ''): Promise<ConfirmSheet[]> {
  const response = await request(`/api/shard-publish/sheets?contract=${encodeURIComponent(contract)}`)
  if (!response.ok) throw new Error('确认单读取失败')
  const payload = (await response.json()) as { items: ConfirmSheet[] }
  return payload.items
}

export function getSheet(sheetNo: string): Promise<ConfirmSheet> {
  return request(`/api/shard-publish/sheets/${encodeURIComponent(sheetNo)}`)
    .then((response) => response.json() as Promise<ConfirmSheet>)
}

export async function listDeliveries(contract = ''): Promise<Delivery[]> {
  const response = await request(`/api/shard-publish/deliveries?contract=${encodeURIComponent(contract)}`)
  if (!response.ok) throw new Error('交付记录读取失败')
  const payload = (await response.json()) as { items: Delivery[] }
  return payload.items
}

export function getDelivery(deliveryNo: string): Promise<Delivery> {
  return request(`/api/shard-publish/deliveries/${encodeURIComponent(deliveryNo)}`)
    .then((response) => {
      if (!response.ok) throw new Error('交付详情读取失败')
      return response.json() as Promise<Delivery>
    })
}

export function continueDelivery(deliveryNo: string): Promise<ActionResult<Delivery>> {
  return postJson(`/api/shard-publish/deliveries/${encodeURIComponent(deliveryNo)}/continue`)
}

export function receiveDelivery(
  deliveryNo: string,
  payload: {
    receipt_token: string
    written?: Record<string, string[]> | null
    interrupted?: boolean
    operator?: string
  },
): Promise<ActionResult<Delivery>> {
  return postJson(`/api/shard-publish/deliveries/${encodeURIComponent(deliveryNo)}/receive`, payload)
}

export function verifyDelivery(deliveryNo: string): Promise<ActionResult<Delivery>> {
  return postJson(`/api/shard-publish/deliveries/${encodeURIComponent(deliveryNo)}/verify`)
}

export async function listTargetRows(target = ''): Promise<Record<string, unknown>[]> {
  const response = await request(`/api/shard-publish/target-rows?target=${encodeURIComponent(target)}`)
  if (!response.ok) throw new Error('目标接收行读取失败')
  const payload = (await response.json()) as { items: Record<string, unknown>[] }
  return payload.items
}
