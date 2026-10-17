# Madeira Epic Games 接入方案

日期：2026-10-11。状态：用户已批准整套方案，并要求直接实现、不影响Steam；实现和真机验收进行中。

## 用户意图与第一版

用户希望 Madeira 增加 Epic Games。参照当前 Steam 入口，默认提议第一版完成：用户登录自己的 Epic 账号，查看已购/已领取且有 Windows 版本的游戏，在 Madeira 下载、暂停/继续下载，安装完成后直接启动，并使用现有分辨率、手柄和触控设置。用户也可选择缩小到导入已经下载好的 Epic 游戏。

完整账号版本的验收必须包括真实登录、真实游戏库、一个小型已拥有游戏的完整下载，以及何轲 iPad 上的实际启动。服务接口测试、编译通过或新增入口均不能代替这些验收。

Epic 云存档、商城购买/自动领取、好友聊天和额外第三方启动器接入不在提议的第一版内。Steam 登录、下载、Dock 和云存档保持原有行为。

## 用户看到的流程

1. 设置中增加 Epic Games 账号入口，打开 Epic 官方登录网页，密码和验证码只在 Epic 网页输入。账号可单独退出。
2. 登录后，游戏库增加 Epic Games 分组，展示账号可安装的 Windows 游戏、封面及安装/下载状态。无 Windows 版本或需要其他启动器的条目给出准确状态。
3. 游戏详情提供下载和暂停/继续按钮，显示下载进度、所需磁盘空间及错误原因。完全校验并安装完成后才允许作为已安装游戏启动。
4. Play 复用 Madeira 当前 JIT 与 Wine 启动流程，使用 Epic 清单中的真实启动文件、工作目录与启动参数。
5. Epic 要求的当次启动授权由账号模块获取，不进入用户的永久启动参数。登出不会删除游戏或存档。

网页登录回调是否可直接返回 iOS App 尚未验证。实现默认允许在 Epic 官方页面登录后，将一次性授权码粘贴回 Madeira；只有验证过实际客户端回调后，才宣称能自动返回。此项是实现提议，不是用户已明确同意的体验。

## 实现方式

采用原生 Swift/Foundation 网络和 SwiftUI 界面，使用现有 Keychain 模式存储 Epic 会话；不在 iOS 内运行 Python、Electron 或桌面 Epic Games Launcher。参考 Legendary/Heroic 已实现的协议与 Epic 下载清单格式，使用用户自己的 Epic 授权访问 Epic 服务和内容服务器。

Epic 的公开 EOS 账号 SDK不能直接视为完整商城游戏库和游戏下载接口。Legendary 使用的商城/Launcher 接口属于第三方集成，可能变化；登录、游戏库和下载请求失败必须明确呈现，不能显示虚假的成功或擅自切换账号。

建议模块为 Epic account/API、owned library、BuildPatch manifest/chunks、install records/download model、launch authorization 和 Epic views。现有 Steam 模块继续负责 Steam；不为两个来源引入不必要的大规模通用平台重构。

库标识使用 Epic 的字符串 appName/namespace/catalog ID，不能复用 Steam 数字 AppID。游戏配置中的来源字段须可选或兼容解码，以保留已有本地游戏和 Steam 游戏设置。安装记录和缓存按来源与 Epic 账号隔离，下载文件位于 drive_c 下独立的 Epic 安装目录。

下载器要解析实际 BuildPatch 清单和 chunk 格式，验证 chunk 与最终文件摘要，再写入安装完成记录。支持取消、断点、网络恢复和受限内存占用。账号变更时取消旧账号的在途工作；不能把先前账号的响应或清单应用到新账号。

后台下载沿用项目现有 iOS 后台处理模式：在系统允许时继续，权限或处理时长结束时安全保存进度并暂停，返回 App 后可继续。不承诺无限后台运行；Epic 使用独立的后台任务标识，或协调已有队列，不能与 Steam 同时抢占同一个任务。系统中止、锁屏、账号退出和重启恢复都列入测试。此项为遵循现有项目模式的实现选择。

## 安全与现有启动接点

- 刷新凭据只进设备 Keychain；网页密码、授权码、exchange code 与 ownership token 不进日志、诊断包、游戏配置、剪贴板自动记录或文件缓存。
- 当前启动桥接会打印 argv，游戏详情也展示持久化参数。Epic 必须增加非持久化的当次授权参数通道，并对敏感参数、拼接参数和包装命令进行脱敏。
- Epic 游戏命令行必须遵循 Windows 参数转义规则，不能靠不受控字符串拼接；秘密授权参数与用户普通参数区分，防止用户参数覆盖当次授权。
- 清单中的路径、启动文件和工作目录必须验证，限制在对应安装根内；处理路径穿越、绝对路径、大小写冲突、符号链接和损坏/超限清单。
- 授权请求只发送到预期 Epic HTTPS 主机；游戏 CDN 请求和跳转不得携带账号 Authorization header 到其他域名。
- 不自动购买、领取、删除用户游戏、替换存档或修改其他设备的认证/配对。

## 测试与交付

用户补充的磁盘约束：纯IPA解包、资源/依赖/摘要检查及报告放在外盘
`/Volumes/untitled/github/Madeira/app/Build/Products/IPAInspection/` 的本任务子目录；
实际必须使用APFS的编译、strip或其他步骤使用内盘独立临时目录。完成后将必要
产物与证据归档外盘，再及时清理本任务创建且已无进程使用的内盘目录；保留已有
游戏、存档、包及共享构建缓存。具体流程见 `docs/BUILDING.md`。这是已明确的用户
约束；用户随后已批准Epic第一版功能范围及网页登录回填方案。

先写针对实际协议结构与生产解析代码的测试：授权过期/取消/账号切换，分页与重复库条目，BuildPatch 压缩与 chunk/file 摘要，损坏清单/路径攻击，下载中断与续传，部分安装不得显示完成，Windows 参数转义和所有日志/预览脱敏。回归现有游戏配置解码、Steam 登录/启动和普通 EXE 启动。

通过独立代码与安全审查及 iOS 构建后，再测试何轲 iPad。用户在设备 Epic 网页亲自完成登录，不在聊天中提供密码或验证码。选择一个小型、账号已拥有且无额外启动器的 Windows 游戏完成下载和运行验证；重启 Madeira 后验证会话和已安装记录恢复。

未完成真实账号和下载验证时，报告为未验证；若登录接口、授权、设备解锁或游戏本身兼容性阻塞，则报告具体步骤与所需用户操作。Epic 接入不能视为 FM26 已修复。

## 独立设计检查

独立建设者审阅提出三个问题：完整接入与导入模式的范围、一次性授权码回填是否可接受，以及后台下载行为。前两项需用户确认；后台行为已按现有 iOS 机制具体化，不引入无限后台运行承诺。审阅未产生产品代码改动。

## 参考与归属

- Legendary：https://github.com/legendary-gl/legendary
- Heroic：https://heroicgameslauncher.com/faq
- Epic EAS：https://dev.epicgames.com/docs/epic-online-services/accounts-and-social/eos-epic-account-services/getting-started?lang=en-US

协议参考与代码移植应明确区分。若移植 Legendary 的 GPLv3 代码，保留上游归属及条款，不擅自附加 Madeira Converter Exception；法律/第三方说明与代码交付保持一致。
