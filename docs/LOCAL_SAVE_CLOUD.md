# 本地游戏 iCloud 存档

更新时间：2026-10-12。此功能加入本地 **0.1.3/build16**，完整编译、宿主测试、
代码和安全审查、IPA/签名/资源验证通过，主 IPA 已更新为16；当前设备安装仍为
build15。build16 安装、真实
iCloud 传输、两台设备恢复、文件选择器及确认按钮交互尚未验证。

已验证的独立包为
`app/Build/Products/IPAInspection/local-save-cloud-build16/Madeira-0.1.3-build16-GameMode-and-iCloudSaves.ipa`，
大小 **203303864 字节**，SHA-256
`4467177c4ff06645bb4d9ce805005a9398bf2e27392b87473236d9fbf3802f70`。
旧 build15 包保留，尚未替换或重启设备上的运行版本。

## 适用范围

入口是游戏库 **Settings → iCloud Drive → Local game saves**。仅处理通过
Madeira 游戏库手动添加的本地游戏，并且必须逐个指定实际存档目录。没有配置
目录的游戏不会自动上传、恢复或覆盖；保存在 Windows 注册表里的进度不包含在内。

Steam 和 Epic 游戏条目不参与此功能。Steam Cloud 保持原有模块，Epic Cloud
仍未实现。这里的云目录也不是原有 **Saves** 页的 ZIP 导入目录；新功能使用独立
的目录快照与恢复代码，不复用旧 ZIP 导入器。

## 首次配置

1. 关闭游戏并等待运行结束，再打开 **Local game saves**。
2. 点 **Choose iCloud Drive folder…**，在“文件”选择一个专用的 iCloud Drive
   文件夹。Madeira 在其中建立 **Madeira Local Saves** 子目录。它通过 Files
   授权的目录访问和最小书签保存选择，不新增 iCloud entitlement 或注册容器。
3. 在 **Local game** 选择游戏。点 **Choose this game's save folder…**，进入
   **我的 iPad/iPhone → Madeira → wine → drive_c** 选择存档文件夹；也可以输入
   Windows 路径，再点 **Use this save folder**。

允许的范围是游戏可执行文件所在目录或其子目录，以及 Windows 用户目录下的
游戏专用 **Documents**、**Saved Games** 或 **AppData/Local、LocalLow、Roaming**
子目录，例如 `C:\users\user\Saved Games\ExampleGame`。应选择实际存档文件夹；
选择安装目录本身会把该目录中符合规则的文件也纳入快照。

不能选择整个 C 盘、Windows 目录、Users/用户根目录、整个 Documents/AppData
目录或已知 Steam/Epic 客户端账户目录。路径穿越、链接、不安全文件以及已知
账户文件也会被拒绝。这些规则不是对任意游戏文件中敏感内容的自动识别保证；
同步范围由明确选择的游戏存档目录决定。

可选择其他 Files 提供商，但其最终上传、离线和跨设备行为由该提供商决定。
选择了一个普通本地目录，并不会让它自动变成 iCloud 云目录。

## 上传存档

点 **Upload saves**，检查游戏名称和文件数，然后确认 **Upload new cloud version**。
每次上传建立新的不可变版本，之前的云版本保留。确认时会重新核对本地内容和
云版本列表；预览后发生变化，需要重新查看并确认。

**Save backup written to the selected folder. Cloud upload has not been confirmed**
表示备份已经写入所选目录，但不能证明服务器收到。只有实际的
`ubiquitousItemIsUploaded` 状态为真，才显示 **iCloud reports this save backup is
uploaded**。可以在“文件”查看同步状态；不要把本地写入成功当作另一台设备已能下载。

## 下载或在另一台设备恢复

1. 在另一台设备添加同一款本地游戏，并配置它的实际存档目录。
2. 选择同一个 iCloud Drive 文件夹，点 **Refresh cloud backups**。
3. 选定本地游戏，在目标版本点 **Download into selected game…**。
4. 核对 **Cloud game**、**Local game**、**Destination**、文件数及将替换的现有
   文件数，再点 **Back up and restore**。

恢复前会保留原目录快照，位置为 **文件 → Madeira → iCloud Save Backups**。
云版本中存在的文件合并到已配置目录；本地额外文件保留。预览的替换计数与实际
恢复使用相同的 Windows/Unicode 路径比较规则。若确认前本地或云内容变化，
操作会拒绝旧预览。恢复使用完整暂存目录和原子替换；不支持安全替换的位置会
保留原存档并报告错误。

这需要用户明确匹配云版本和本地游戏，不会仅凭名称自动覆盖。云文件尚未下载
到设备时，先等待 Files 完成下载；超时会说明备份暂不可用。

## 运行协调与容量限制

存档操作和游戏启动互斥。同步前暂停两家商店下载，并等待实际传输任务停止；
已有 Steam Cloud 写入仍在运行时不会抢占。取消操作会等待在途文件 I/O 结束，
再释放同步占用。游戏真正结束后才能同步，不以关闭页面或隐藏游戏视图代替。

每个备份最多 **1,024 个文件、单文件 16 MiB、内容总量 64 MiB**；编码后的
输入备份上限为 **96 MiB**。每台设备最多配置 **256 个本地游戏目录**。云子目录
扫描限制为 **512 个非 `.`/`..` 目录项**；完整版本各占数据和元数据两个文件，
额外目录项也占预算。达到限制时拒绝继续写入，不自动删除已有云版本。

这不是持续后台自动同步，也不保证无网络、后台限时或提供商故障时完成传输。
未配置保存位置的游戏与注册表进度不会被自动迁移。

## 实现与验证记录

- `app/Madeira/LocalSaveSnapshot.swift`：有界目录快照、SHA 核对、路径比较、
  原存档备份及暂存合并恢复。
- `app/Madeira/LocalSaveCloud.swift`、`LocalSaveCloudView.swift`：目录配置、Files
  授权书签、不可变云版本、确认流程、提供商状态及手动操作界面。
- `app/Madeira/GameSessionWork.swift`：游戏/下载/存档的占用和停止边界。

实际生产代码的快照核心在 Debug/优化构建下各通过 **117** 项断言；云模块各
通过 **58** 项及 **5** 个可编译运行的保护失效对照；游戏工作协调各通过 **40**
项及 **8** 个运行对照。独立代码、安全审查通过，Steam/Epic 宿主回归通过。

证据分别在外盘 `app/Build/Products/IPAInspection/local-save-snapshot/validation-final.json`、
`local-save-cloud-tests/result.json`、`game-session-work/final-validation.json` 和
`local-save-cloud-security-review/approval.json`。这些本机 APFS/文件提供商边界
测试没有访问真实 iCloud 账户，不是云服务器或两台设备的通过证明。

纯 IPA 解包检查仍放在外盘任务目录。确实需要 APFS 的测试使用独立、已记录的
内盘临时目录，归档必要证据后及时清理；参见
[构建及临时存储规则](BUILDING.md#temporary-storage-for-ipa-checks-local-workflow)。
