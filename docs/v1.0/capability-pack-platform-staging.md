# Capability Pack 与平台 Runtime 的产品化交付边界

本文记录 v1.0 的真实构建、安装与恢复合同。它不是发布成功证明；GA
证据只能由受保护的 Windows/macOS runner、签名服务和真实安装演练产生。

## 1. 一个版本、二十四棵输入树、一个原子激活单元

受保护的 platform-stage 对同一 commit 生成固定二十四棵树：

- Windows x64、macOS arm64、macOS x64 各一棵 Core；
- 每个目标各一棵 Bootstrap；
- 每个目标各一棵 browser、channels、image、ocr、office、sandbox Pack。

Candidate 只接受同一仓库、同一 commit、同一成功 `workflow_dispatch`
产生的完整二十四棵树。每棵树分别绑定内容清单、目标平台、stager
身份、workflow run、固定 gate 证据和 supply-chain 清单。少一棵、目标
重复、路径碰撞、链接/reparse point、内容变化或 gate 缺失都会停止签名。

三个目标的公开生产 Runtime 配置以 Base64 + SHA-256 存入受保护
Environment 变量。单个 Base64 值不得超过 GitHub 官方 48 KiB
边界，即解码后最多 36 KiB；它只能包含最终会随 Core 发布的公开
配置，不得包含凭证或模型私钥。作业开始后在 `runner.temp` 内排他
创建、按摘要复验，结束时由 `always()` 摘要围栏删除。

用户机器上的激活单位不是四个互相独立的目录，而是一个 slot：

```text
slots/<slot_id>/
  .release-package                 # 已签名 Core 压缩包原字节
  release-manifest.json            # 已签名 ReleaseManifest
  .slot.json                       # Core + Pack 复合收据
  payload/
    bin/ecorex[.exe]
    bin/pack-python/...
    pack-python.json
    runtime-config.json
    web/...
    web-manifest.json
    capability-packs/
      browser/<原始签名 zip + json 文件名>
      channels/<原始签名 zip + json 文件名>
      image/<原始签名 zip + json 文件名>
      ocr/<原始签名 zip + json 文件名>
      office/<原始签名 zip + json 文件名>
      sandbox/<原始签名 zip + json 文件名>
```

Core 与六个 Pack 必须同 release、version、build digest、platform 和
architecture。不存在“Core 已切换、Pack 稍后补装”的产品状态。

## 2. Core 与 Pack 的执行边界

Core 包含可重定位 Python 3.11 闭包、ASGI Runtime、平台 launcher 和最终
React 资源。`pack-python.json` 同时绑定解释器路径、解释器 SHA-256、闭包
文件数、总大小和整棵树摘要；生产 resolver 不回退到 `sys.executable`
或 PATH。

browser Pack 只暴露固定的 navigate/snapshot/click/type/wait/screenshot
与受限 fetch 操作。它不提供任意 JavaScript evaluate。Playwright Python
分发与受保护 runner 上安装的 Chromium 被封装进 Pack 内部的
`browser-runtime.zip`；内部 manifest 绑定每个成员、浏览器可执行文件和
归档摘要。每次调用临时展开、逐文件复验，并在成功、失败、超时路径关闭
browser/context/playwright 生命周期。

image Pack 只是 `ecorex-managed-image-bridge-v1` 握手。它明确返回
`provider_execution=false`，不能持有云端模型密钥，也不能绕过 Core 的
Managed Image Orchestrator 执行生图或修图。

channels Pack 绑定飞书与腾讯文档的连接器契约、结果 Artifact 信封和登录
HITL 边界，不把第三方 SDK 或凭证交给模型。ocr Pack 封装本地 RapidOCR /
ONNX 闭包，office Pack 封装文档、表格、演示和 PDF 的渲染闭包；两者的
输出都先进入后端 Artifact/lineage 合同，不能直接向前端泄漏中间文件。

sandbox Pack 只运行平台固定 shell，并逐字段确认 Core 下发的 sandbox
contract。Windows Core 自带 AppContainer + Job Object helper；
workspace-write 不授予网络 capability，临时 ACL 在完整进程树退出后恢复，
Job Object 使用 kill-on-close。danger-full-access 仍经过同一 helper 持有
进程树与超时，但不伪称 AppContainer 隔离。macOS 使用系统
`/usr/bin/sandbox-exec` Seatbelt policy，读写仅映射签名解释器、Pack 与
workspace，网络默认拒绝。候选构建必须通过真实越界读写、网络和子进程
逃逸探针。

Pack 不能自行注册新的工具或服务合同。工具包必须由外层
ReleaseManifest、内层 CapabilityPackManifest 和 Core 编译的 ToolSpec
digest 三者共同授权，之后才由受信任产品 adapter 绑定 handler。依赖服务
包则绑定 Core 内置的 service contract digest；它们不会伪装成模型可调用
工具，也不会绕过 Capability/Policy/Artifact 边界。

## 3. 原子下载、验证与激活

正式 ReleaseManifest 对当前 host 必须包含固定十二个 Pack artifact：六份
archive 与六份 sidecar。只要 manifest 声明任何 Pack，就进入严格模式，
缺失或多出的 host Pack 均在创建安装事务前拒绝。

下载按同一签名 manifest 的顺序执行：GH 国内镜像、GitHub Releases、
EcoreX CDN。`transactions/<transaction_id>/pack-install.json` 为十二个文件
分别记录 source index 与 queued/downloading/verifying/retrying/verified，
因此崩溃后只续传尚未验证的文件。某一源返回坏字节时删除该临时文件并
切换下一源，不会把 Core 的来源选择误当成 Pack 的验证结果。

每份 Pack 经过两层验证：

1. ReleaseManifest 对 archive/sidecar 的 artifact 签名、文件名、大小和
   SHA-256；
2. sidecar 的 Ed25519、pack/version/target/runtime API、archive 文件名、
   大小、SHA-256 与 ToolSpec binding。

更新域只定义 `PackContentVerifier` 协议，不导入 Capability 域。产品组合
层显式注入校验 adapter；Bootstrap CLI、Runtime update composition、
provisional activation 和 current-slot verifier 使用同一实现。声明 Pack
却没有 adapter 时必须 fail-closed。

全部文件验证后，`SlotStore.stage` 先在临时目录解开 Core，再把六组原始
签名 Pack 文件投影到 payload，生成复合收据，然后才原子 rename 为候选
slot。收据同时保存：

- `core_payload_digest`：签名 Core 解包内容；
- `payload_digest`：加入六个 Pack 后的完整 payload；
- `supplemental`：每个 Pack 的 ID、签名 artifact ID、原始文件名和摘要。

复验时不能把本地 `.slot.json` 当作签名根。Runtime 会从复合 payload 中
剔除固定 `capability-packs/` 投影，重新计算 Core 子树并与保留的签名 Core
归档比较；六个 Pack 再分别走双层签名验证。即使同时修改 Core 文件和本地
`payload_digest`，也不能重新授权被替换的 Core。

此时 current pointer 不变。用户点击“更新并刷新”后才 drain/checkpoint、
migration dry-run、写 provisional activation intent 并切换 current。
Bootstrap 在启动前、解析 executable 后、确认健康前再次复验完整 slot 与
六份 Pack。任何 Pack 缺失、替换、增加文件或内容变化都拒绝启动。

候选在健康确认和 data barrier 前失败时恢复 prior pointers 并删除候选；
data barrier 后只允许 roll-forward。STAGING 已完成 rename 但复验失败时，
协调器会主动删除未受保护候选，避免重试复用半成品。

## 4. 兼容性与禁止状态

为了读取已经签名的旧测试/迁移 manifest，完全不含
`capability-pack-*` artifact 的 ReleaseManifest 被明确识别为
`legacy Core-only`。这是有名称的兼容分支，不是缺 Pack 时的降级路径。
v1.0 Candidate recipe 和 platform stage 固定要求二十四棵树，因此正式发布
不能进入该分支。

以下状态均禁止：

- manifest 有一部分 Pack，客户端把它当作 Core-only；
- Pack 校验失败后继续激活 Core；
- Runtime config 指向安装根目录外或使用简写/可变 Pack 文件名；
- browser/sandbox Pack 回退到系统 Python、PATH executable 或未签名 helper；
- image Pack 直接访问 provider；
- 前端决定 Pack 是否可用或自行拼接工具执行命令。

## 5. 受保护 runner gate 与本地证据边界

仓库拥有 stager、Windows helper、Windows/macOS launcher、Pack 源码和
探针。三个 Environment-gated GitHub 托管 runner 使用固定 OS 标签并提供
正确架构的 MSVC/clang；工作流安装锁定的 Python profile、最终 Web dist
与真实 Chromium。生产 `runtime-config` 以 Base64 Environment 变量传输，
在 `${{ runner.temp }}` 中做 SHA-256/JSON/重复键校验后才交给 stager，并在
`always()` 阶段删除。运行期间不满足任一条件都会生成 typed failure，
不会生成“占位通过”收据。

platform-stage 依赖入口固定为
`scripts/install-v1-python-profile.py --profile platform-stage`，权威清单为
`requirements/locks/manifest.json`，并由
`scripts/check-v1-dependency-locks.py` 复验。Core 的 dependency-closure 与
每棵 stage 的 supply-chain receipt 都记录 manifest/profile lock SHA-256、
inventory mode 和 package count；Core 必须 complete，browser、OCR 与
Office 只允许该 profile 的受约束 subset，版本不一致直接失败。

制品上限按身份区分：Core 保持 150 MiB，Bootstrap 保持 10 MiB，Pack
archive 为 500 MiB，Pack sidecar 为 1 MiB。本机真实 Chromium 1.58 闭包
的内部归档测得 208.21 MiB；旧的全局 150 MiB 限制已拆除，但没有因此
放宽 Core。

核心 gate 包括 launcher、loopback activation health、完整 pack-python
闭包和 supply-chain；browser 包括真实 Chromium snapshot、任意 evaluate
拒绝、进程隔离和 supply-chain；channels 包括连接器契约与 schema smoke；
image 包括 managed bridge 与 provider 拒绝；OCR/Office 分别实际执行识别
与四类办公格式生成/读取；sandbox 包括真实平台边界和 process-tree
containment。

当前 Windows 开发机现已具备固定 Go 1.26.5、MSVC tools 14.44.35207
（compiler 19.44.35227.0）和 Windows SDK 10.0.26100.0。精确 main
`701aa422...dce600` 的本地演练实际编译了 Windows launcher/AppContainer
helper，并完成 Core、Bootstrap 和六 Pack 的八份本地 receipt；摘要见
`evidence/windows-signed-candidate-main-2026-07-16-summary.json`。这只证明
工具链和产品路径在该工作站可执行，不证明 protected clean-runner 身份，
也不提供 macOS launcher、Seatbelt 或三平台固定二十四 receipt。对应 GA
证据仍必须来自受保护 Environment 的原始 receipt；旧的非发布观察值继续
保留在 `evidence/platform-pack-local-2026-07-11.json` 供追溯。

当前聚焦回归命令：

```powershell
python -m pytest -q `
  tests/v1/test_platform_pack_staging.py `
  tests/v1/test_process_capability_pack.py `
  tests/v1/test_shell_sandbox_boundary.py `
  tests/v1/test_candidate_release_pipeline.py `
  tests/v1/test_atomic_pack_install.py

python -m pytest -q `
  tests/v1/test_update_coordinator.py `
  tests/v1/test_update_durability.py `
  tests/v1/test_update_activation_health.py `
  tests/v1/test_bootstrap_supervisor.py
```
