# Web 客户端稳定请求身份与恢复 Outbox

状态：v1.0 已实现（2026-07-11）

## 根因与边界

旧 Web 客户端在 `RuntimeClient.createTurn/steer/queue/replace` 内部临时生成
`client_message_id`。一旦服务端已经提交、但 HTTP 响应在客户端收到前断开，用户点击
重试就会生成第二个身份，服务端无法判断它是重试还是第二条消息。新会话还会先创建一个
没有可恢复身份的空 Thread，进一步放大重复会话问题。

v1.0 把身份所有权上移到调用方。一次用户意图在发出任何网络请求前创建不可变
`ClientOperation`，其中固定：

- `operation_id` 和 `client_message_id`；
- 内容指纹、输入、Agent/图片模型；
- Thread 目标，以及创建新 Thread 时使用的同一个 `client_request_id`；
- 创建操作时观察到的事件水位；
- steer/queue/replace 对应的活动 Turn 快照和实际 disposition；
- 创建时间与 72 小时失效时间。

`RuntimeClient` 不再代替调用方生成消息身份，也不允许同一个操作被改投到另一个既有
Thread 或另一个活动 Turn。

## 两阶段新会话恢复

```text
stage ClientOperation
  → POST /threads（operation_id 作为 client_request_id）
  → 原子记录 resolved_thread_id
  → POST /threads/{id}/turns（固定 client_message_id）
  → projection/event 确认 client_message_id
  → 一次性移除 outbox 记录
```

任一阶段“服务端已提交、客户端未收到响应”时，页面重载后都会重放同一个请求身份：

- Thread 创建由服务端 `client_request_id` 幂等返回同一个 Thread；
- Turn、steer、queue、replace 由 `client_message_id` 幂等；
- steer 始终重试原活动 Turn，绝不因当前 UI 状态变化而改成新 Turn；
- 只有 Thread 投影中的 Turn 或事实事件携带相同 `client_message_id` 才算确认；普通
  HTTP 成功但尚未取得事实确认时，记录仍保留。

启动完成和浏览器 `online` 事件都会尝试恢复未确认操作。恢复期间 Composer 暂停新提交，
避免恢复与新点击并发。首次发送失败时草稿不清空，按钮明确显示“重试发送”。

## sessionStorage 数据最小化

Outbox 使用版本化键 `ecorex:v1:client-operation-outbox`，单次 `setItem/removeItem`
完成状态替换。约束为：

- 最多 16 条未确认操作；
- 整体最多 96 KiB；
- 单条输入最多 24 KiB；
- 默认 72 小时失效，读取时清理过期、损坏、超版本或超界数据；
- 只存重放请求必需的文本、模型和目标身份；
- 不存 bearer/CSRF/连接器凭证、附件二进制或路径、服务端响应、产物和工具结果；
- sessionStorage 不可用或超限时 fail closed，并向用户说明消息尚未安全入队。

同一个 `operation_id` 或 `client_message_id` 再次 stage 时，只有完整合同与指纹均一致才
视为重试；任何内容、模型、目标或 disposition 漂移都会在本地拒绝。

实现位于按需加载的 `desktop/src/v1/deferred/clientOperationOutbox.ts`。首屏仍只加载
Runtime 和基础工作区；bootstrap 完成后才读取恢复队列。发布资源哈希器同时补上了对
压缩代码中 template interpolation 内动态 import 的解析，确保该恢复模块被纳入内容寻址
DAG，而不会成为漏签名的孤立文件。

## 验证留痕

`desktop/src/v1/api/runtimeClient.test.ts` 覆盖：

- 操作对象及嵌套目标/模型不可变；
- 重复点击只保留一条记录，同身份不同 payload 本地拒绝；
- Thread 和首条消息分别发生“提交后丢响应”，重载后仍复用原身份；
- resolved Thread 跨重载保留，避免重复空会话；
- 投影/事件确认后一次性移除；
- 超量拒绝、过期清理、事件游标与严格事件校验；
- Web TypeScript 合同检查。
