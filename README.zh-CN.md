# bStocks LP Assistant

[English](README.md) | [中文](README.zh-CN.md)

一个面向 Binance Web3 **bStocks** 资金池的波动率感知型 LP 顾问——完全建立在
**Binance MCP / Agent OS 框架**之上：Binance 的 Web3 市场数据 API，加上
Agentic Wallet（`baw`）MCP/CLI 接口，没有其他数据源或执行路径。

专门限定在 bStocks（RWA 列表 `type=3`，代币符号以 `...B` 结尾，例如
`TSLAB`、`NVDAB`）——不包括 Ondo（`...on`）或 xStocks（`...x`），这些是同一
标的下的其他代币化股票提供方。`fetch_stock_tokens()` 默认只取 bStock，所有
命令都默认继承这个范围（显式要求的话，`stocks --type` 可以浏览其他提供方）。

## 基于 Binance MCP 构建——逐模块对应

| # | 能力 | 用到的 Binance Agent OS 模块 |
|---|---|---|
| 1 | 通过 Binance 的 **MCP/API 拉取市场数据** | Binance Web3 APIs——公开的 RWA 股票代币列表 + 链上K线（无需鉴权） |
| 2 | **基于波动率计算不同区间的 IL/APY，并给出存款建议**——怎么获取"安全且高"的 APY | 本项目的模型（`riskscreen.py`）：盈亏平衡波动率评分 + 区间扫描（见下方"核心思路"） |
| 3 | 通过 Agentic Wallet **直接操作存款/取款** | `binance-agentic-wallet` 已确认的 `defi deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove`——本项目把执行环节转交给它，不重复实现 |
| 4 | 通过 Agentic Wallet **读取全局投资情况** | `baw defi position`——持有的LP仓位，过滤到代币化股票交易对 |

具体展开成四部分：

1. **市场数据**——直接从 Binance 的 Web3 API 和 Agentic Wallet（`baw`）CLI
   拉取股票代币价格/K线，以及 LP 池的 APY/TVL/组成。
2. **风险调整后的建议**——基于波动率计算无常损失成本，覆盖满区间和一组集中
   流动性区间扫描（对称做市区间，或单边限价单式区间），用盈亏平衡波动率比
   率给池子评分，推荐真正"安全且高APY"的区间，而不只是APY高的那个。
3. **执行**——刻意不在这里重复实现。存取款走 `binance-agentic-wallet` 已经
   审查过的、需要确认的 `defi deposit` / `defi lp-add` / `defi redeem` /
   `defi lp-remove` 流程，使用本工具给出的 `investmentId` 和代币地址。
4. **持仓 + 再平衡检查**——通过 `baw defi position` 读取当前 LP 持仓，把它
   们的 vol_ratio 和实时市场比较，生成报告（绝不自动执行交易——见下文）。

**第一次用？** 看 [INSTALL.md](INSTALL.md)，了解怎么把它接入你自己的
Claude + Agent OS 环境，用对话的方式使用，不用记命令行语法。

## 核心思路

*完整推导、每条公式对应到具体实现函数：[MODEL.md](MODEL.md)（GitHub 本
身就能原生渲染里面的公式）。下面是精简版。*

LP 的手续费/激励 APY 本质上是对无常损失（IL）的补偿，而 IL 的大小取决于池
子资产的波动率。股票代币池子特别适合这样建模：它们都是和稳定币配对，所以
IL 几乎完全由股票代币自身的波动率决定——不需要像加密货币对那样考虑跨资产
相关性。

对恒定乘积做市商（AMM），标准的扩散近似给出：

```
E[IL] ≈ σ² / 8      （年化，满区间 / V2风格流动性）
```

其中 `σ` 是代币的年化波动率，用链上K线历史估算。

**波动率估计器**：对于稳定币计价的池子，`σ` 现在来自 **Yang-Zhang 估计器**
（Yang & Zhang，2000年，*"Drift-Independent Volatility Estimation Based on
High, Low, Open, and Close Prices"*，Journal of Business）——它用上了K线数
据里本来就有的OHLC四个价格，而不是只用收盘价，具有漂移无关性（价格有趋势
时也不会有偏），在相同样本量下比纯收盘价方法统计效率高约5-14倍（参考
range-based估计器的文献，也可参见
[Parkinson 1980](https://www.jstor.org/stable/2352357) 和
[Garman-Klass 1980](https://doi.org/10.1086/296083)，Yang-Zhang 就是在这些
基础上发展出来的）。当OHLC数据看起来退化时，会回退到纯收盘价方法
（`annualized_volatility`）。非稳定币配对目前仍然对价格*比率*用收盘价方
法——文献里没有针对两个资产比率的标准OHLC估计器，这是一个明确记录下来的
范围局限，不是疏忽。

### 科学的比较方法：盈亏平衡波动率

满区间做市在数学上等价于持续对冲一个**平值跨式期权空头**——手续费APY就是
卖出波动率收到的权利金。这给出了一种恰当的方式来回答"在不同波动率和APY组
合下，到底哪个池子更划算"——和期权交易员比较隐含波动率与已实现波动率是同
一个问题：

```
σ*（盈亏平衡波动率） = √(8 · apy)         -- 使 apy 恰好抵消 E[IL] 的波动率
vol_ratio             = σ_realized / σ*    -- <1：池子给的比实际风险要求的更多（溢价高，划算）
                                            -- >1：手续费收入很可能覆盖不了实际观察到的风险（溢价低，不划算）
```

`vol_ratio`——命名为 **Richness Score（丰厚度评分）**，分档为 **Rich**
（`<0.5`）/ **Fair**（`0.5–1.0`）/ **Cheap**（`>=1.0`）——是**区间无关**的：
集中度会把手续费收入和IL按同一个倍数放大，所以在盈亏平衡方程里正好抵消。
这是池子本身APY相对于代币波动率的固有属性，跟你打算用哪个区间去持有它无
关——这比单纯看APY排名、甚至比单看 `model_net_apy` 数字，更能做到跨池子（APY/波
动率量级差异巨大时）的苹果对苹果比较。

### 集中流动性（V3）区间：做市 vs 限价单

在 `[Pa, Pb]` 上的集中头寸表现得像一个加了杠杆的满区间头寸——手续费收入和
IL 都会被同一个 Uniswap V3 集中度倍数 `M = 1 / (1 - √(Pa/Pb))` 放大。

- **跨价区间**（`Pa < 1 < Pb`，默认模式）：普通的集中流动性做市。`p_active`
  = 整个周期内从未离开区间的概率——现在是精确闭式解（反射法/镜像法级数解，
  不是近似），在测试套件里用蒙特卡洛路径模拟验证过，见下方"注意事项"。
- **单边区间**（`Pa ≥ 1` 或 `Pb ≤ 1`）：**收益增强型限价单**——卖出方向的
  区间只有当价格涨进区间后才会开始转换成稳定币（并赚取手续费）；买入方向
  的区间则是价格跌进去之后。这里 `p_active` 的含义不一样——是*订单最终会成
  交的概率*，用单边界触及概率计算——这个数字通常比跨价情形的"从未离开"概
  率要高。

工具会对每一侧扫描一组候选区间/偏移量，在"一年内保持活跃的概率 ≥60%"的候
选里推荐 `model_net_apy` 最高的那个——这个60%的门槛就是"安全且高APY"里"安
全"的那一半；更窄的区间可能显示更高的 `model_net_apy` 数字，但 `p_active`
低到不能算"安全"。两个数字总是一起展示，绝不只给APY。`range` 还会对推荐区间做一个
**情景压力测试**——用同一个 `[Pa, Pb]`，把估算的 `σ` 分别乘以1x/1.5x/2x
（Neutral/Elevated/Stress）重新算一遍，因为 `σ` 本身就是回顾性的估计值；
这样能看出这个建议到底有多依赖"波动率估计是对的"这个前提，而不是把一个数
字当成确定的事实呈现出来。

### 不是所有池子都是稳定币计价的

`NVDAB-BNB`、`BNB-SPCXB`、`HOODB-BNB` 等池子是拿 bStock 和 BNB 配对，不是
和稳定币配对。**这是一个真实的bug，不是文档里说明过的简化**：早期版本对所
有池子都只用bStock自身的波动率算IL，等于悄悄把计价资产当成了完全不动的稳
定币，即便它其实在动——这会低估每一个非稳定币配对池子的风险。现在
`resolve_pool_stock_and_quote()` 会根据池子链上的 `assetTokenList` 判断配
对类型，对于非稳定币计价的池子，`relative_annualized_volatility()` 会用两
边资产按时间对齐的K线，计算 `log(P_stock / P_quote)` 的波动率——这种池子的
IL取决于两个被质押资产之间的相对变动，不是任何单一一个资产自己的波动率。
每条结果都带一个 `pair_mode`（`"stablecoin"` / `"non_stablecoin"`）；
`scan`/`range` 会明确给非稳定币结果打标签（`[non-stablecoin pair]`），不
会让它看起来和稳定币配对的结果一样。

稳定币地址列表本身现在放在 [`stablecoins.json`](stablecoins.json) 里，
不再写死在脚本里——编辑这个文件（或者用 `BSTOCKS_STABLECOIN_CONFIG` 环
境变量指向另一个文件）就能新增/修正一条记录，不需要改代码。不在这个列
表里的地址永远不会被当成稳定币——只会走上面的相对波动率路径，而这条路
径对一个真正的稳定币计价来说也会收敛到同一个答案（因为它自己的波动率
接近0）。配置文件缺失或格式错误会失败到一个空集合（并打印警告），而不
是崩溃，也不是悄悄沿用一份可能已经过期的内置兜底列表。

## 每一个结果都是四种结论之一

这个工具报告的每一个池子最终都会落到 **`ENTER`** / **`WATCH`** /
**`NO_TRADE`** / **`UNSCOREABLE`** 这四种结论里的恰好一种——一个统一的
`verdict` 字段，在 `scan`/`range`/`recommend`/`rebalance-check` 之间保
持一致，而不是每次都要从散落各处的grade/flags/vol_ratio字段里重新拼凑
出同一个判断：

- **`ENTER`**——通过了交易门槛：`model_net_apy` 为正，**并且**
  `vol_ratio < 1`（不是Cheap档）。这是 `recommend` 的"Top pick"必须过
  的那道槛。
- **`WATCH`**——通过了存款前安全筛查（是个合法的池子），但现在过不了
  交易门槛——净APY为负，或者评级是Cheap。这不是一个警告，只是*现在*不
  划算；值得留意（万一apy/波动率的情况变了），而不是要回避它。
- **`NO_TRADE`**——没通过存款前的安全/合理性筛查（见下文），或者（专
  门针对 `recommend`）压根没有任何候选能通过交易门槛。在这道门槛存在
  之前，即便所有候选池子扣完IL都是负收益，或者评级都是Cheap，
  `recommend` 依然会打印一个"Top pick"——这读起来像是一种从没打算做出
  的背书。当没有任何候选能通过门槛时，`recommend` 会打印一个明确的
  `NO_TRADE` 结论，说明具体原因，只把最接近门槛的候选列出来作参考（明
  确标注"这不是推荐"）——新增的是，还会告诉你有多少个池子停在
  `WATCH`，而不是对它们完全沉默。
- **`UNSCOREABLE`**——压根没被评估过（抓取失败、确认不了是bStock、K线
  数据不够）。特意跟 `NO_TRADE` 区分开："我们不知道"和"我们查过了，不
  值得"是两个不同的结论，把它们混在一起（早期版本就是这么做的）会让人
  没法区分"这个池子有风险"和"我们压根没数据"。当市场里 `UNSCOREABLE`
  的比例太高时，`recommend` 也会直接拒绝给出任何结论——见下文"可靠
  性"。

## rebalance-check：给一个具体的替代方案，不只是评级对比

之前只是把持有池子的评级和"市场上最好的评级"作对比，只能告诉你*可能*有
更好的选择存在，但说不清具体是哪一个、换过去到底划不划算。现在
`rebalance-check` 会为每个持仓指出一个具体的最佳替代池子
（`best_alternative`：协议、TVL、apy、`model_net_apy`、`vol_ratio`），并
计算一个具体的换仓结论（`switching`）：用持仓自身的美元价值（来自
`defi position`）乘以 `model_net_apy` 的差值，算出一个年化美元差距，再
拿这个差距去对比 `ASSUMED_SWITCH_COST_USD`（一个明确写出来的假设——大约
2美元的BSC gas成本，覆盖一次"移除流动性+重新添加流动性"的往返操作——这
是一个声明出来的假设，不是实测出来的成本，而且**不是完整的换仓成本**：
它不包含当前实时gas价格，也不包含退出那一刻实际实现的无常损失，因为这
个工具没有入场价/成本基础数据去算出那部分）。只有当估算的回本周期能在
`SWITCH_PAYBACK_DAYS_WORTHWHILE`（30天）之内，才会给出"换"的结论；否则
`rebalance-check` 会明确说**按兵不动**，同时把差距和回本估算展示出来
——这是一个有理由的"不值得"，不是沉默。`needs_attention` 现在直接由这个
结论驱动，不再是一个裸的vol_ratio倍数启发式规则（只有在持仓自身的apy根
本算不出来时——比如它被标记了——才会退回用那个规则当兜底）。

## 存款前风险与合理性筛查

V3/V4 池子可能报出极不靠谱的 `apy` 数字，而且没有哪一个单一字段检查能覆盖
所有出问题的方式——所以每个池子都会过一遍 `pool_risk_flags()`，一组*独立*
的信号，回答两个问题：**收益是不是真的**，以及**这个池子安不安全**。任何
一个信号触发，这个池子就会被排除出排名，并且把原因报告出来，不是悄悄丢掉。

| 信号 | 回答哪个问题 | 抓的是什么 |
|---|---|---|
| `feeRate` > 5%/笔 | 收益是真的吗？ | 动态/keeper定价的费率快照，静态年化公式处理不了 |
| TVL < $5,000 | 收益是真的吗？ | 流动性太少导致apy统计上很吵，容易被单笔交易左右 |
| apy 超过同ticker中位数的5倍 | 收益是真的吗？ | **通用情形**——不管是什么机制造成的，已知的还是未知的 |
| `investable = false` | 安全吗？ | 已下架，无法新存款 |
| 协议 `securityScore` < 50 | 安全吗？ | 明显不靠谱的协议（较弱的下限，见下文） |
| 协议是V4系列（`defiProtocolId`） | 安全吗？ | **默认硬性拦截**——见下方"Uniswap V4 池子默认硬拦截" |

**和 `flagged` 是两回事：`unscoreable`。** 有些池子根本没被评估过——
`investment-info` 抓取失败、`assetTokenList` 确认不了是bStock、或者没有
足够的（重叠）K线历史。`scan`/`recommend` 会把这些单独列出来，不跟
`flagged` 混在一起——"我们没法评估这个"和"我们评估了、它不安全/不合理"是
两个不同的结论，把两者混成一次静默丢弃（早期版本就是这么做的），会让人没
法区分"这个池子有风险"和"我们压根没数据"。

### Uniswap V4 池子默认硬拦截

V4 池子可以挂任意自定义hook合约——审计过的核心AMM之外的逻辑。这个工具没
有API能拿到池子的hook地址、权限或审计状态，协议级别的 `securityScore` 也
看不到这个（V3和V4分数完全一样，见下方局限说明）。与其把这个留成一条注意
事项，现在默认把**每一个**V4池子都排除出排名，无条件——不只是那些已经有
明显症状（比如极端feeRate）的。要覆盖这个行为，明确要求或者是已经审查过
的池子，可以传 `--allow-v4 "理由"`——它现在需要带一个理由，不是一个裸标
志，这个理由会原样记录在 `--json` 输出的 `v4_override_reason` 字段里：
既然要覆盖的这道拦截本来就是因为这个工具看不见hook风险才存在的，那覆盖
它的时候就该留下一条"为什么"的记录，而不只是留下"有人这么做过"。更深层
的修复（真正的hook审计，等有这个数据之后）还在Roadmap里。

促成这个机制的案例：一个 Uniswap V4 的 QQQB-USDC 池子显示
`apy=1,658.77%`，而同一交易对的等价 V3 池子只有 77.86%——追查发现是
`feeRate` 为 `8.38861`（每笔838.86%，不是有效的费率档位）。单靠 `feeRate`
检查只能抓住*这一种*机制；同ticker异常值检查（这个池子的apy是同ticker中
位数的21倍）独立地标记了同一个池子，事先根本不需要知道具体原因——这正是
这一节想说明的"泛化"。

**不一定是恶意的。** [Fables](https://www.fables.fi)——一个跑在 Uniswap v4
上、原生集成hook的 ve(3,3) DEX，同样交易代币化股票——用的是"智能费率"：
hook 会根据已实现波动率、日历/交易时段状态或订单流方向，逐笔重新定价手续
费，有边界限制、由 keeper 驱动；他们自己的文档把显示的费率描述为"最新的合
约读数，不是能约束后续交易的报价"。这种池子的 `feeRate` 快照是一个实时的
瞬时数字——我们的静态费率年化方法，不管这个hook本身是否合法，本来就是用
错了工具。这个 flag 的含义是"我们没法给这个算出有意义的apy"，不是指控。

**值得说清楚的局限**：`securityScore` 来自 `defi protocol-info`，是按*协
议*算的，不是按池子或按hook算的——Uniswap V3 和 V4 的分数都是95.18，因为
是同一个组织。这个信号在QQQB那个案例上完全没有预警。它是针对明显不靠谱协
议的一道下限，不是hook审计——真正更深层的V4-hook安全项还在Roadmap里，这
个信号替代不了它。

## 注意事项（相信这些数字之前先读一下）

- **`model_net_apy` 是一个模型估计值，不是承诺或历史回报**——它是平台的
  `apy` 减去本工具估算出的IL成本，特意命名为 `model_net_apy`（不是
  `net_apy`）就是为了不让它读起来像一个保证能拿到的回报。另外：平台自己
  的 `apy` 数字本身也只是一个单一的混合数字。直接对照过 `defi
  investment-info` 在采样的每一个bStock池子上返回的内容——那个API里完全
  没有手续费和激励的拆分，没有时间戳，也没有任何锁仓/赎回/激励到期字段。
  这是一个确认过的数据可得性局限，不是疏忽：一个靠激励撑起来的高apy，可
  以一直看起来很诱人，直到激励计划结束的那一刻，而这个工具没有任何办法
  提前看到这一点。每一次 `scan`/`recommend` 输出都会打印这条注意事项
  （`--json` 里对应 `model_apy_caveat` 字段）。
- **历史波动率是回顾性的。** 股票代币在财报、分红、拆股、停牌前后容易跳
  空——进任何一个池子之前，查一下 `binance-tokenized-securities-info` 的
  资产市场状态API，看有没有即将发生的公司行为。从短K线历史算出来的
  `vol_ratio` 是个带噪声的估计，不是精确数字。
- **跨价区间的"从未离开"概率现在是精确闭式解**（反射法/镜像法级数解，不
  是近似），在测试套件里用直接的蒙特卡洛路径模拟验证过。它替换掉了之前
  更宽松的并集上界近似——那个近似对窄区间会显示 `0%`，哪怕真实概率其实是
  一个不大的正数。
- **单边（限价单）区间还在借用IL-vs持有公式当成本代理**——还没有建模相对
  于边界处普通限价单的真实平均成交价。这是一个已知的简化，没有隐瞒。
- **没有漂移项。** 所有公式都假设预期价格漂移为零。这是一个波动率风险估
  计，不是方向性预测。

## 结果可视化

在 Claude 里使用时，结果会以图表形式展示（`range` 是安全性-收益散点图，
`scan` 是一个vol_ratio对比apy的气泡图，气泡大小按TVL、颜色按grade区
分），不只是一张原始表格——见 `SKILL.md` → "Visualizing results"。示
例，来自真实的 NVDAB-USDT 数据：

*（图表：横轴 p_active，纵轴 model_net_apy，每个候选区间一个点，推荐的±50%区间
高亮显示——在 Claude 会话里实际使用这个 skill 可以看到渲染出来的版本）*

## 代码结构

仓库根目录的 `riskscreen.py` 只是一个5行的启动器（`from bstocks_lp.cli
import main`）；实际实现在 `bstocks_lp/` 包里，按层次拆分，新功能加在哪
里一目了然：

| 模块 | 职责 |
|---|---|
| `config.py` | 常量、JSON输出的统一信封格式、稳定币地址配置加载 |
| `api.py` | 最底层I/O：HTTP客户端（`_get`）和 `baw` CLI子进程封装 |
| `market_data.py` | 拉取和整理市场数据——股票代币、K线、LP池子列表、持仓 |
| `volatility.py` | 已实现波动率估计器（Yang-Zhang OHLC、收盘价、相对波动率） |
| `il_model.py` | 盈亏平衡波动率、Richness Score（`vol_ratio`）、扩散近似的IL |
| `range_model.py` | 集中区间数学：集中度倍数、留在区间内的概率、`recommend_range` |
| `risk_screen.py` | 存款前风险与合理性筛查，独立于收益模型 |
| `scan.py` | `run_scan`——共用的评估流水线——以及ENTER/WATCH/NO_TRADE/UNSCOREABLE结论判定 |
| `cli.py` | 七个CLI命令、参数解析、`rebalance-check`的展示层辅助函数 |

依赖方向是单向的（`config -> api -> market_data -> volatility ->
{il_model, range_model} -> risk_screen -> scan -> cli`），并且每一次跨模
块调用都走限定名（`market_data.fetch_stock_tokens(...)`，而不是 `from
bstocks_lp.market_data import fetch_stock_tokens`）——这个约定是
`test_riskscreen_integration.py` 里 `monkeypatch.setattr(<模块>, "<名字
>", fake)` 能打到每一个函数的关键，不只是现在已经被mock的那几个。模型
本身见 MODEL.md；这张表只是代码地图。

## 使用方式

```bash
# --- 单一入口：一句话结论（需要已登录的 `baw` 会话）---
python riskscreen.py recommend [--capital 10000]

# --- 市场数据（公开API，无需鉴权）---
python riskscreen.py stocks --limit 20 --type 1
python riskscreen.py vol --ticker TSLA --days 30 --apy 0.30

# --- 排名/建议（需要已登录的 `baw` 会话）---
python riskscreen.py scan --top 15 [--with-range] [--json] [--capital 10000] [--allow-v4 "理由"]
  [--max-pages 3] [--max-fee-rate 0.05] [--min-tvl 5000] [--peer-outlier-multiple 5]
python riskscreen.py range --investmentId <id> [--side straddle|sell|buy] [--allow-v4 "理由"]
  [--target-offset 0.15] [--band-width 0.10] [--capital 10000]
python riskscreen.py range --ticker TSLA --apy 0.30 --side sell   # 不查真实池子也行

# --- 持仓 + 再平衡（需要已登录的 `baw` 会话）---
python riskscreen.py positions [--refresh] [--json]
python riskscreen.py rebalance-check [--json] [--max-pages 3] [--allow-v4 "理由"]   # --json 用于定时监控
```

`recommend`、`scan`、`range --investmentId`、`positions`、`rebalance-check`
都会调用 `baw`（`defi investment-list` / `investment-info` / `defi position`
/ `defi protocol-info`），所以需要一个已登录的 Agentic Wallet 会话（`baw
auth signin` / `baw auth verify`）。`stocks`、`vol`、`range --ticker/--apy`
直接打公开的 Binance Web3 接口，不需要鉴权。`--allow-v4` 用来覆盖默认的V4
硬拦截（见上文"Uniswap V4 池子默认硬拦截"）——只在明确要求时才传这个。

**`--json`（在 `scan`/`positions`/`rebalance-check` 上）是 stdout 上纯粹
的JSON**——所有进度/诊断信息都走 stderr，每个返回值都带 `as_of`（UTC时间
戳）,`scan` 还额外带 `elapsed_seconds`、`flagged`、`unscoreable`。可以直接
喂给 `jq` 或调度器，不用先把表格文字剥掉。

**测试**：`pip install -r requirements.txt && pytest test_riskscreen.py
test_riskscreen_integration.py` 总共跑160个测试。`test_riskscreen.py`
（132个）覆盖纯数学/纯逻辑函数，完全不涉及I/O。
`test_riskscreen_integration.py`（28个）端到端地跑
`run_scan`/`cmd_*`——包括真实的评估流水线、JSON输出、CLI参数解析——只
mock了两个最底层的I/O函数 `baw()` 和 `_get()`，用按调用签名分发的假实
现；中间的每一个 `fetch_*`/`resolve_*`/`evaluate_*` 函数都是真的在跑。
覆盖了：一个干净池子的完整流程、一个锁定了真实发现的fixture（
PancakeSwap Infinity 的V4识别案例，这样这个bug就不会悄悄回归）、非稳
定币配对、不支持的池子结构、格式错误/失败的API响应、`baw()` 的非零退
出码和shell元字符拒绝路径（不需要真的起一个子进程）、每个命令的
`--json` 输出有效性（能解析成纯JSON，stdout上没有别的东西）、
`recommend` 的 `NO_TRADE`/拒绝阈值/仓位读取失败路径、`rebalance-check`
的多investmentId和best_alternative路径，还有两个跑在几百个随机输入上
的property风格测试（没引入 `hypothesis` 依赖——纯用 `random` 驱动的循
环），分别确认 `no_exit_probability` 永远不会跑出 `[0, 1]` 区间、
`_switching_recommendation` 的美元差距符号永远和它给出的结论一致。CI
（`.github/workflows/test.yml`）现在在 `ubuntu-latest` **和**
`windows-latest` 的矩阵上跑这两个文件加 `py_compile`——Windows那一条腿
才是真正跑到了 `baw()` 里那段Windows专属的 `cmd.exe` 包裹逻辑，之前这
段代码在CI里完全没有覆盖，尽管它是这个文件里安全最敏感的一段分支。另
外有一个独立的 `lint` job，装的是
[`requirements-lint.txt`](requirements-lint.txt)（特意没放进
`requirements.txt` 里——装 `ruff`/`mypy`/`pip-audit` 的依赖树会让
`pip install -r requirements.txt` 明显变慢，网络快的情况下也要多花约
19秒，而测试本身根本用不到这些工具），跑 `ruff`（选择了哪些规则、为什
么，见 [`ruff.toml`](ruff.toml)）、`mypy`（`check_untyped_defs`，见
[`mypy.ini`](mypy.ini)——搭建过程中就抓到了 `_get()` 里一个真实的类型
不一致问题）、以及针对两个requirements文件的 `pip-audit`。每个命令的
真实数据冒烟测试记录在下面的"运行状态"里。

**执行环节故意不在这个脚本的范围内。** `rebalance-check` 只打印报告，不会
挪动任何东西。要对某个建议采取行动，走 `binance-agentic-wallet` 的 `defi
preview` → 跟用户确认 → `defi deposit` / `defi lp-add` / `defi redeem` /
`defi lp-remove`，用这个工具打印出来的 `investmentId` / 代币地址。具体流程
见 `SKILL.md` → "Executing a recommendation"。

## 可靠性：重试、错误分类、拒绝给出一个站不住脚的结论

公开的HTTP接口（RWA股票列表、K线）现在都走 `_get()`，遇到瞬时性失败（超
时、连接错误、5xx、429限流）会先用带抖动的指数退避重试几次，才会真正放
弃；构造查询字符串也从原来的裸字符串拼接换成了 `urllib.parse.urlencode`
（裸拼接遇到值里带 `&`/`=`/空格的情况会把请求搞坏，甚至悄悄丢掉一个参
数）。429以外的4xx错误不会重试——重试也不会自己变好；格式错误或形状不
对的响应体也不会重试——这更可能是API契约本身出了问题，不是网络抖动。不
管哪种情况，最终抛出的错误都会带上URL、状态码和尝试次数，而不是一个裸
的 `urllib` 报错堆栈。

`run_scan` 并发抓取池子信息和K线时，现在会分类记录*为什么*某次抓取失败
了（超时/网络错误/数据无效/其他），而不是把所有失败都塞进同一句笼统的
"数据不足"里——这条原因会区分"K线抓取这一步本身就失败了"和"抓到了，但
数据太薄/太不对齐，不敢信"（见上文"给 relative_annualized_volatility 加
固"），这样一批瞬时超时就不会读起来和某些池子真的缺历史数据一样。
`scan --json` 现在带一个 `failure_summary`——对 `unscoreable` 原因做的
频次统计——不用逐条读完所有池子的消息就能看出"出的是什么问题、有多少
个"。

`recommend` 现在会在超过 `UNSCOREABLE_RATIO_REFUSE_THRESHOLD`（50%）的
候选池子完全评估不了时，直接拒绝给出结论——打印一个 `NO_TRADE`，说明市
场里有太大一部分数据缺失，而不是自信地从剩下能评估的那一小撮里挑一个
"Top pick"（这更像是系统性问题的症状——网络问题、`baw` 会话问题——不是
"剩下的这些就足够有代表性"的理由）。

`fetch_stock_tokens()` 和 `fetch_klines()` 现在在进程内做了缓存
（`functools.lru_cache`）——代币列表和某个代币的K线在一个命令的生命周
期内本来就不会变，但之前照样会被重复抓取（`recommend` 之前会在
`run_scan()` 内部已经抓取过一次之后，为了检查持仓又单独再调一次
`fetch_stock_tokens()`）。这不是跨调用持久化的缓存，也没有TTL——每次CLI
调用都是一个全新的进程，缓存在每次运行开始时都是空的，不存在过期问题。
池子状态/APY/TVL（`defi investment-list` / `investment-info`）刻意**没
有**缓存，哪怕在同一次运行内也没有——这类数据变化快，正是一个风险筛查
工具该看到的东西，为了一个远不如下方 `ThreadPoolExecutor` 并发修复来得
显著的提速，去牺牲这个工具本该做对的那件事，不划算。

每一个JSON输出——`scan`/`positions`/`rebalance-check`，不管成功还是出
错——现在都通过 `_json_envelope()` 共享同一套外壳：`schema_version`、
`status`（`"ok"`/`"error"`）、`run_id`、`as_of` 永远都在，命令自己的字
段（`results`、`positions`、`error`……）叠加在上面。以前出错路径只打印
一个裸的 `{"error": ...}`，这些字段一个都没有，而成功路径压根没有明确
的 `status` 字段（只能靠"没有 `error`"这件事来推断）——这是一个真实存
在的不一致，不是吹毛求疵：调度器解析这个输出，成功和出错两条路必须用
不同的逻辑，才能可靠地区分开。

## 性能

每一次 `baw` 调用都是单独起一个 Node.js 进程（纯启动开销约0.6秒，还没算真
实的API延迟）。`scan`/`recommend` 需要给每个候选池子都打一次这样的调用，
串行的话大概是 `0.6秒 × 池子数量`——分页（见下文）扩大候选池范围之后这个
问题就很明显了。`run_scan` 的池子信息和K线抓取现在改成并发执行了
（`concurrent.futures.ThreadPoolExecutor`，上限 `MAX_CONCURRENT_BAW_CALLS
= 8`），不再是一个个来。同一台机器上实测：`scan --top 5`（默认3页扫描，约
39个候选池）**43.7秒 → 10.1秒**；`recommend`（默认1页）**降到约7秒**。如
果还是觉得慢，`--max-pages` 是最主要的杠杆——每多扫一页大概多拉100个候选
池。

用同样的方法（对真实调用做profiling，不是靠猜）又抓到并修好了两处"本
来互相独立、却串行执行"的循环：

- **协议安全评分查询**（`defi protocol-info`，每个不同的协议查一次）之
  前是在 `run_scan` 打分那个循环里一个个查的——每次都是单独一次 `baw`
  子进程。实测：4个不同协议串行查要2.91秒，改成并发后（跟池子信
  息/K线用同一套 `ThreadPoolExecutor`）只要最慢那一次的约0.73秒。在完
  全相同的代码路径上做了修复前后的配对对比
  （`run_scan(max_pages=1, with_range=True)`）：**9.37秒 → 7.10秒**。
- **分页拉取池子列表**（`fetch_lp_investments`）之前是在一个 `for` 循环
  里一页页顺序拉的——这些调用同样互相独立，不需要等对方的结果。现在只
  用第1页就能拿到 `pools_total`，然后只并发拉真正可能有数据的那些页
  （`min(max_pages, ceil(pools_total / 100))`，绝不多拉不存在的页）。实
  测：3页、300个池子，1.39秒拉完（第1页顺序 + 后两页并发），而以前是3
  次完全顺序的子进程调用。

这两处改动都是纯粹的内部执行顺序调整——数据和结果都没变，跑过完整测试
套件，也拿真实数据端到端跑过一遍确认输出跟改动前一致。如果还是觉得
慢，`--max-pages` 依然是最主要的杠杆——每多扫一页大概多拉100个候选
池，`MAX_CONCURRENT_BAW_CALLS` 则控制这些抓取里同时跑几个。

## 运行状态

已经跑通完整流程，针对真实的 `baw` 会话验证过。真实输出
（`python riskscreen.py scan --top 8 --with-range`，BSC，2026-09-02）：

```
pool                ticker        apy      vol  grade best +/-%  range-net  confidence           tvl
GMEB-USDT           GME       426.06%   23.81%   Rich       50%    913.40%        High        19,848
AAPLB-USDT          AAPL      364.19%   32.57%   Rich       50%    646.12%    Moderate        54,040
GMEB-USDT           GME       256.48%   23.81%   Rich       50%    549.18%        High       136,619
AAPLB-USDT          AAPL      219.90%   32.57%   Rich       50%    388.89%    Moderate       441,293
NVDAB-BNB           NVDA      125.82%   39.59%   Rich       50%    178.25%    Moderate        42,924
BNB-SPCXB           SPCX      125.87%   82.78%   Rich      full    117.30%        High     1,303,516
HOODB-BNB           HOOD      123.21%   71.84%   Rich      full    116.76%        High        17,299
NVDAB-USDT          NVDA      104.17%   39.59%   Rich       50%    146.78%    Moderate        67,397

1 pool(s) excluded from ranking -- anomalous feeRate:
  QQQB-USDC (Uniswap V4): feeRate=838.86% per swap outside a sane range, and apy is
  21.3x the peer median -- see "Pre-deposit risk & plausibility screen"
```

这8个上榜的池子全部评级为 **Rich**——高得吓人的APY确实反映了相对于已实现
波动率的可观溢价，不只是数字看起来很大而已。被排除的第九名（QQQB-USDC，
Uniswap V4）说明合理性过滤器在正常工作，不是排名漏掉了它。

NVDAB-USDT 的区间扫描（`range --investmentId 9c97dee1...d405de7ec7f79d`）：
Richness Score 评级 **Rich**（`vol_ratio` 0.18）。满区间净APY 59.18%，置信
度 **High**；推荐的±50%区间置信度降到 **Moderate**，净收益84.23%。同一个
池子的 `--side sell` 扫描显示紧贴价格上方±5%的区间置信度 **High**（活跃期
间净APY超过1000%）——这就是迭代历史里"用LP区间当限价卖单"那个用例。

## Binance Agent OS Mini Hackathon —— Track A 提交

打包成了一个 [Skill](SKILL.md)——由 AI agent（这个仓库从头到尾由 Claude 构
建和驱动）按需加载，对应 Agent OS 的"Skill Hub"这一块。直接映射到 Agent OS
自己的几大支柱（[binance.com/en/agent-os](https://www.binance.com/en/agent-os)）：

| Agent OS 支柱 | 本项目对应 |
|-------------------------|------------------------------------------------------------------------|
| **读取市场** | Web3 APIs——公开的RWA股票代币列表 + K线，用于波动率/IL模型 |
| **追踪你的持仓** | `baw defi position`——持有的LP仓位，过滤到股票代币交易对 |
| **链上操作** | 建议交给 Agentic Wallet 的 `defi deposit`/`lp-add`/`redeem`/`lp-remove`，每次都要确认——不在这里重复实现 |
| **Skill Hub** | 打包成 `SKILL.md` + 脚本，和 Binance 自己发布的skill（`binance-agentic-wallet`、`binance-tokenized-securities-info`）同样的形态 |

这个产品的核心主张：把"LP的APY是多少"变成"这个APY值不值得承担这个风险，
应该用什么区间"——一个把股票代币LP当作它本来就是的做市（或限价单）行为来
对待的波动率感知顾问，用期权交易台给跨式期权定价的方式给池子评分，给出一
个具体的区间而不是含糊的"APY看着不错"，并且始终停留在顾问的角色上——每一
笔资金的实际挪动都还是人工确认的 `baw` 调用。

## Roadmap

波动率/IL模型给*市场*风险定价。上面的存款前筛查现在能抓住不合理收益和明显
不靠谱协议的情况了，但它仍然是数据合理性+声誉检查，不是*池子合约*安全审
计——一个池子可以通过上面所有信号，但如果合约本身有问题，依然不安全。补
上这个缺口是近期的优先级；下面按和已有内容的关联紧密程度大致排序。

### 还没做的

- **Uniswap V4 hook 安全审计。** 临时缓解措施已经上线（默认硬拦截所有V4
  池子，见上文）——还缺的是能让这个拦截"选择性放开"而不是一刀切的真正审
  计能力：某个具体池子有没有挂hook、它的审计状态、权限、是否命中已知恶意
  hook名单——跟这个项目已经对波动率风险采取的"不要直接推荐存款"这个谨慎
  态度是一回事，只是往合约风险这一层再深挖一步。`query-token-audit` 已经
  有针对代币的这种检查模式；池子层面的对应物还是空白。在这之前，
  `--allow-v4` 只是人工覆盖，不是替代品。
- **历史校准/回测。** 这里每一个区间/情景数字都只是"此刻"的模型估计
  （`--json` 输出里的 `as_of`）——没有一个被拿去跟后续实际发生的情况对照
  过。一套纸面交易或历史回放机制（记录一个建议，N天后拿真实的池子/价格数
  据回头验证）能验证或纠正模型的假设（扩散近似、把平台apy当满区间基准处
  理、非稳定币配对的相对波动率模型）,而不是让这些假设一直停留在没验证过
  的理论层面。这个比清单上其他项目的工作量都大得多——这一轮先标记出来，
  没有动手做。
- **Robinhood Chain 兼容性。** 已经有实锤，不是空想：
  [Fables](https://www.fables.fi) 已经在 Robinhood Chain 上线，交易代币化
  股票（NVDA/USDG、TSLA/USDG、AAPL/USDG、SPY/USDG……），背后有一个公开的
  Blockscout 浏览器 `robinhoodchain.blockscout.com`——Blockscout实例通常都
  会暴露 REST/GraphQL API，这是一条可行的近期路径，可以像
  `fetch_stock_tokens`/`fetch_klines` 对接Binance的bapi那样去拉可比的市场
  /池子数据。如果那边的数据确实可比地拿得到，把 `stocks`/`vol`/`scan` 扩
  展过去，就能把这个工具从"只看币安内部"升级成"跨平台比价"——对回答"同一
  个标的，哪个平台的LP更划算"这种问题真正有用，而不只是平台内部选哪个池子。
- **分时段波动率。** Fables 的"Calendar"费率模型之所以在交易时段/盘后/周
  末/假期分别定价，正是因为代币化股票的交易行为在这些时间窗口确实不一样
  （标的资产只有在纽交所/纳斯达克开盘时间才有真实的做市）。
  `annualized_volatility()` 目前把所有K线当成一个同质序列处理；把已实现波
  动率拆成"正常交易时段" vs "盘后"两段（用
  `binance-tokenized-securities-info` 的市场状态API给每根K线打标签），大概
  率能让 `vol_ratio` 和区间建议更精准——这正是Fables专门做一套费率模型的原
  因。
- **单边区间的真实成交价模型**——现在 `--side sell`/`buy` 区间还在借用
  IL-vs持有公式当成本代理；真正的问题（"相比边界处的普通限价单，我实际的
  平均成交价是多少"）需要自己的模型，不是借来的。
- **波动率不确定性感知评分。** `vol_ratio` 目前仍然用的是已实现波动率的
  点估计（现在是Yang-Zhang而不是纯收盘价——见上文"最近完成的"——但无论如
  何还是一个点估计）；K线历史短的话这个估计会很吵。在计算 `vol_ratio` 之
  前用置信区间放宽 `σ`（已实现方差的抽样分布是卡方分布，所以这个可以推出
  闭式解），能避免薄弱的数据窗口显得过于精确。再往前一步是完整的
  GARCH/EWMA预测模型，实现成本更高，而在这么短的K线历史上收益也更不确定。
- **自动化公司行为拦截**——"硬性告警"这个想法（公司行为/脱钩/流动性崩
  溃/激励到期）具体能落地的第一步。现在"查一下有没有即将发生的公司行
  为"这一步只是 `SKILL.md` 里的人工提醒；应该直接调用
  `binance-tokenized-securities-info` 的资产市场状态API，在财报/分红/拆股
  日期落在建议的时间窗口内时，自动放宽有效波动率估计或者直接标红这个池子。
  脱钩/流动性崩溃/激励到期这几类告警各自需要自己的信号源，目前还停留
  在想法阶段，没有细化方案。
- **决策快照。** 把每一次 `recommend`/`scan` 的结论（输入、`verdict`、
  背后的数字）记到一个持久化的本地存储里，以后可以回头对照实际发生的
  情况看它靠不靠谱——"这个结论后来站不站得住"。需要一个schema，也需要
  一个这个无状态CLI脚本现在还没有的持久化层；值得认真设计而不是随手拼
  一个上去，而且跟下面的历史校准这一项关系密切（一个快照存储基本上就
  是纸面交易harness需要的大部分东西，可以拿来回放）。

### 最近完成的（移除 MODEL.pdf——它占了安装体积的大头）

同样是"安装感觉慢"这个反馈，但原因不一样：这个仓库 git 实际追踪的文
件总共约1.15MB，光 `MODEL.pdf` 一个文件就占了817KB（约71%）——比整个
`bstocks_lp/` 包加上 `riskscreen.py`（约140KB）加起来还大5倍以上，而
这只是一份排版好看的便利副本，skill 运行时根本不会读它。现在已经从
git 追踪的文件树里移除，`MODEL.md`（GitHub 本身就能原生渲染里面的
`$...$`/`$$...$$` 公式）是唯一保留的版本。这等于撤销了上一次明确要求
（生成PDF并推送）的一部分——先把这个取舍摆出来跟用户确认过，而不是自
己替他们做决定。

### 最近完成的（把 riskscreen.py 拆成一个 bstocks_lp/ 包）

用户直接提出的要求：安装和运行都感觉慢。没有靠猜，而是对一次真实的
`recommend` 式调用做了profiling（39次 `baw` 调用，9.37秒），发现还有两
处循环跟"性能"一节里已经修过的并发问题是同一个模式——互相独立、无状态
的 `baw` 调用却在一个个顺序执行：`run_scan` 里按协议查安全评分的循环，
以及 `fetch_lp_investments` 拉第2页及之后的循环。现在都用上了跟池子信
息/K线抓取一样的 `ThreadPoolExecutor` 模式。实测的修复前后对比见上面
"性能"一节。新增2个测试（共160个，此前158个）。

### 最近完成的（把 riskscreen.py 拆成一个 bstocks_lp/ 包）

用户直接提出的要求：既然以后还会陆续加功能，先把架构理顺。
`riskscreen.py` 已经长到2055行——HTTP/子进程I/O、波动率估计器、
IL/盈亏平衡模型、区间模型、存款前筛查、扫描流水线、外加全部七个CLI命
令，全挤在一个文件里。现在拆成了分层的 `bstocks_lp/` 包（见上面"代码
结构"），`riskscreen.py` 缩成一个5行的启动器——`python riskscreen.py
<命令>` 的用法完全不变，也没多一道安装步骤。

拆分时最要命的约束：`test_riskscreen_integration.py` 通过
`monkeypatch.setattr(riskscreen, "baw", fake)` 来mock I/O，这之所以能
生效，是因为所有调用方都是在调用时刻去同一个模块命名空间里查找 `baw`
这个名字的。一旦拆到不同文件里，这个机制就会失效，除非每一次跨模块调
用都走限定名（`api.baw(...)`，而不是 `from bstocks_lp.api import
baw`）——这条规则被定成了整个包里所有调用的统一约定，不只是今天被mock
的那六个名字。拆分过程中还顺带抓到并修好了一个本该会被悄悄引入的真实
bug：稳定币配置的默认路径是相对 `__file__` 算的，如果原样搬进
`bstocks_lp/config.py` 会算错一层目录（这种失败是"失败关闭"的——稳定
币集合变成空的，只在stderr打一行警告——现有测试根本抓不住这种问题）。

验证不止跑测试套件：新包整体过了 `ruff`/`mypy`；直接检查过稳定币配置
还能正常加载（地址数量非零）；还拿真实的API/`baw`会话跑了
`scan`/`recommend`/`range`，确认输出跟重构前逐字节一致。

### 最近完成的（独立的模型论文——MODEL.md / MODEL.pdf）

README 里"核心思路"一节一直只是个精简版；用户要求整理一份标准论文格式的
完整数学推导，于是现在有了独立的文档：[MODEL.md](MODEL.md)（摘要加上编号
的第1-10节，每条公式都对应到 `riskscreen.py` 里具体实现它的函数——逐条对
照真实代码核对过，不是凭印象写的），以及从同一份内容渲染出的排版版
[MODEL.pdf](MODEL.pdf)。整理过程中顺带抓到并修好了一处真实的文档漂移：
英文版 README 的"The idea"一节里，跨价区间的 `p_active` 还被描述成"a
conservative union-bound approximation"（保守的联合界近似）——这个说法早
就过时了，好几轮之前就已经升级成精确的双边界反射级数解法（联合界近似现
在只是退化输入时的兜底）；`README.zh-CN.md` 这边其实一直是对的，说明两份
README 在这一点上已经悄悄不同步了一段时间，这次才被发现修正。

### 最近完成的（图表可读性第三轮——标签还是叠在一起）

第二轮的修复（试几个候选偏移方向再加引导线）拿真实、密集的数据一测
还是不够：前10名里有好几个池子在纵轴上只差几个像素，4-6个方向里随
便哪个固定小偏移都躲不开这么近的邻居。这次在`SKILL.md`里做了两处改
动：（1）`scan`图表的横轴（`vol_ratio`）在扫描范围跨度超过约5倍时
（很常见，一次扫描里从<0.1到>2都算常态）改成对数刻度——把真正密集、
有意义的那一簇点铺开，不再让1-2个离群的`WATCH`池子占掉线性轴的大半
宽度；（2）标签摆放不再围着每个点找空隙，而是在图表边缘留一列，按
各点原本的纵向位置排序，用一个贪心的"下压"过程强制保证最小行间距——
不管簇有多密，这样从结构上就保证标签之间零重叠。代价是离自己那一行
比较远的点，引导线会变长——跟同一个真实用户确认过这就是该做的取舍
（"引导线可以更长一点"）。

### 最近完成的（用户问"这些池子涵盖了所有bStock池子吗？"）

现场调查这个问题时发现：全市场一共有496个LP池子，其中40个名字带
bStock，`scan`默认的`--max-pages 3`能看到40个里的38个（漏掉的2个都
是APY接近零的Uniswap V4池子，反正也会被硬性拦截），而`recommend`默
认的`--max-pages 1`看到的就更少了。输出里完全没有提示这件事——被
`max_pages`截断和真正扫完全市场，在结果上看起来一模一样。现在从源
头修复：`fetch_lp_investments`会读取API自带的`total`字段（真实的池
子总数，跟实际抓了几页无关），和抓到的池子一起返回；`run_scan`会返
回一个`coverage`（`scan`/`recommend`）/`market_coverage`
（`rebalance-check`）对象——`{pools_fetched, pools_total, truncated}`
——贯穿每个命令的`--json`输出。文本模式下，只要扫描确实被截断了，
就会打印一行`NOTE: scanned X/Y LP pools...`，所以这个答案现在就在输
出里，不用每次都重新手工调查一遍。

新增3个测试（共158个，此前155个）。

### 最近完成的（引导线——光靠偏移标签还不够）

上一轮的修复（每个点旁边固定方向偏移一个标签）结果发现自己还不够：在
密集的簇里，一个标签照样可能卡在两个点中间，或者跟旁边的标签重叠。
`SKILL.md` 现在明确要求做真正的防碰撞标签摆放（试几个候选偏移方向，
碰到会跟已经摆好的标签重叠就跳到下一个），外加一条引导线——从点的边
缘牵一条细线到标签最终落地的位置——这样不管标签最后摆在哪，点和标签
的对应关系都不会含糊。用同一个真实用户的场景重新渲染确认过效果。

### 最近完成的（实际使用中的第四轮反馈）

- **`SKILL.md` 不再让agent自己编一个存款金额。** 之前的指引说用户没给
  金额时"挑一个合理的示例数字"——真实用户反馈：哪怕标注成"举例"，凭空
  给出一个美元数字也会读成是在假设他们要投多少钱，而这是他们根本没说
  过的事。现在的做法是：默认只给百分比（净APY、IL），只有用户真的给
  了金额之后才算 `$/yr`。
- **"给排名前几个池子"的默认深度从2-3个改成了10个**——但换了个更高效
  的取数方式：用一次 `scan --top 10 --with-range --json` 调用（每条结
  果的 `best_range` 里已经带了推荐区间宽度、置信度、IL、净APY），而不
  是对10个池子分别调用10次 `range --investmentId`——后者会悄悄把两轮
  之前刚修好的"调用慢"问题带回来。现在完整的、覆盖每一档候选区间宽度
  的 `range` 扫描明确限定在一次只看一个池子（Top pick，或者用户具体问
  到的那个），不会一下子摊开到十个池子。
- **气泡图的大小指引收紧了**（最大半径从约24px降到约14px）——真实场
  景里密集的池子簇挤在一起，大到把自己的标签都盖住了——用重新渲染过
  的版本确认过修复效果。

### 最近完成的（实际使用中又发现的三个问题）

- **`range` 的文本表格现在带一个明确的 `il` 列。** 无常损失之前只是隐
  含的（`eff.apy` 减 `net_apy`，从来没作为一个独立数字展示过）——有用
  户希望能直接看到，不用自己去减。跨价区间和单边区间
  （`--side sell`/`buy`）的表格都加上了。
- **`--capital` 现在会给每一档区间都模拟 `$/yr`，不只是推荐的那一
  档。** 之前只有那一个推荐区间有美元数字（在表格下面单独一行），现
  在每一行都有自己的 `$/yr @<金额>` 列——"±20%和±50%之间实际差多少
  钱"变成一眼就能看出来，不用自己心算五次乘法。
- **`SKILL.md` 的指引针对另外两个真实缺口做了加强**：图表上的每个点
  必须有肉眼可见的身份标识（点旁边直接标出来），不能只靠悬停——一个
  技术上正确的气泡图，如果不悬停就看不出哪个泡泡是哪个池子，照样算失
  败。而且每张图/每段回复现在都要求简单解释一下 `vol_ratio`/
  `p_active`/grade/verdict这些术语，不能假设读者已经知道。给出建议
  的时候现在有一条明确的"完整画面"底线：既要有全市场的图，也要有排
  名前2-3个池子的区间/IL/模拟收益对比，不能只孤零零地给一个Top pick。

新增1个测试（总共155个，此前154个）。

### 最近完成的（用户自己测试发现的两个bug）

- **光是为了跑测试而装依赖，也明显变慢了。** 把 `ruff`/`mypy`/`pip-audit`
  加进 `requirements.txt` 之后，网络快的情况下光是安装就要多花约19秒，
  而测试本身根本用不到这些工具。拆成了 `requirements.txt`（只有
  `pytest`，装起来约3.4秒）和
  [`requirements-lint.txt`](requirements-lint.txt)（只给CI的独立
  `lint` job 用）——见上文"测试"。
- **`scan` 建议画的图之前会误导人。** 之前的指引（对原始 `vol_ratio`
  画一个升序排列的柱状图）正好复现了一个真实用户从自己截图里发现的困
  惑：`vol_ratio` 最高（评级Cheap）的那些最差池子，柱子反而画得最
  长——按默认的视觉习惯，这会读成"越长越好"，但实际上 `vol_ratio` *越
  低*才是好的——TVL和安全等级也都被埋进了文字标签里，图上完全看不出
  来。现在换成了 `SKILL.md` 里一个明确写死的气泡散点图规格
  （`vol_ratio` 横轴、`model_net_apy` 纵轴、气泡大小=TVL、颜色=grade，
  外加一条标在 `ENTER`/`WATCH` 分界线上的参考线）——见 `SKILL.md` 里的
  "Visualizing results"。

### 最近完成的（ENTER/WATCH/NO_TRADE/UNSCOREABLE 结论体系）

审查"产品下一阶段"这一档里的第一块——也是审查自己说这一档是长期工作
的——缩小到不需要新的外部数据源、也不需要持久化层就能实打实做出来的
那一部分：一个直接建立在 `passes_trade_gate` 现有逻辑上的统一
`verdict` 字段，不是一个新门槛。完整内容见上文"每一个结果都是四种结论
之一"。

- **每一条 `results`/`flagged`/`unscoreable` 记录现在都带一个
  `verdict`**（`ENTER`/`WATCH`/`NO_TRADE`/`UNSCOREABLE`）——之前只有
  `recommend` 挑"Top pick"的时候才会临时算一下 `passes_trade_gate()`；
  `scan --json` 的使用者之前没有任何字段可以拿来筛选/排序"这个到底值
  不值得"，只有 `grade`（只看vol_ratio，看不出净APY是不是正的）。
- **`recommend` 现在会把 `WATCH` 的池子亮出来**，而不是除了Top pick和
  明确的 `NO_TRADE` 之外全部沉默——排名表里有，`NO_TRADE` 结论下面也有
  一行计数，这样"现在没什么好进的"和"压根没什么值得看的"就能读出是两
  码事了。
- **`scan` 的文本表格加了一列 `verdict`**（`--with-range` 和普通模式都
  有），这样人读表格看到的标签和 `--json` 的使用者解析到的是同一个。

这一轮刻意**没有**动手做的，以及为什么：**决策快照**（记录一次
`recommend`/`scan` 的结论，以后回头看它到底靠不靠谱）需要一个持久化
层，这个无状态的CLI脚本现在还没有，而且schema值得认真设计而不是随手
拼一个上去。**硬性告警**（公司行为/脱钩/流动性崩溃/激励到期）需要接
入一个新的外部数据源（`binance-tokenized-securities-info` 的资产市场
状态API，目前只是 `SKILL.md` 里的一条人工提醒），而且要真正接进自动
化筛查里，不是做一次性检查。**纸面交易/历史回放**是审查自己列出来体
量最大的一项——在这一轮之前Roadmap里就已经标注"已标记，尚未动手"，这
一轮结束后依然如此。这三项都是真正的产品决策，不是bug修复；不先确认
好设计意图就动手做，很容易白费不少功夫做出跟实际想要的东西对不上的
东西。

新增5个测试（总共154个，此前149个），另外给
test_riskscreen_integration.py 里好几个已有的测试加上了 `verdict`
相关的断言。

### 最近完成的（测试与工程化缺口，来自同一轮审查）

审查的第四档，收尾了具体的工程化诉求：真正mock了函数之间的连接逻辑
（不只是函数本身），CI也真正跑到了这个工具自己那项安全修复所在的平台
专属代码。完整内容见上文"测试"，简单说：

- **`test_riskscreen_integration.py`**（22个测试，新文件）——mock了
  `baw()`/`_get()` 之后端到端跑 `run_scan`/`cmd_*`：格式错误/失败的
  API响应、`baw()` 的非零退出码和shell元字符拒绝路径（不需要真的起子
  进程）、每个命令的 `--json` 输出有效性、`recommend` 的 `NO_TRADE`/
  拒绝阈值/仓位读取失败路径、`rebalance-check` 的多investmentId和
  best_alternative路径，还有一个真实fixture（PancakeSwap Infinity）把
  这轮审查里的一个真实发现锁定成了永久性回归测试。
- **Property风格测试**，针对 `no_exit_probability`（200个随机参数组合
  下始终落在 `[0, 1]` 区间内）和 `_switching_recommendation`（美元差距
  的符号永远和结论一致）——没引入新依赖（`hypothesis`），纯用
  `random` 驱动的循环。
- **把 `build_parser()` 从 `main()` 里拆出来**，这样测试就能构造真实
  的argparse `Namespace` 对象（`build_parser().parse_args([...])`），
  而不是手搭一个可能悄悄跟真实CLI契约脱节的假对象。
- **CI矩阵加上了Windows**——之前只有Ubuntu，导致 `baw()` 里那段
  Windows专属的 `cmd.exe` 包裹分支（正是命令注入修复里最安全敏感的那
  条分支）在CI里完全没有覆盖。
- **新增了 `ruff`、`mypy`、`pip-audit`**，作为一个独立的CI `lint`
  job。把两个工具在真实代码库上发现的问题都修了（`_get()` 里一个类型
  不一致的变量、几处缺失的dict类型标注、多余的f-string前缀、一个不必
  要的 `open()` mode参数、几个容易看错的单字母OHLC变量名），而不是加
  了工具就把已有的问题晾在那不管。`ruff.toml`/`mypy.ini` 刻意排除了几
  类跟这个代码库故意选择的风格相冲突的规则（尤其是I/O边界上"broad但
  会被翻译成明确错误"的异常处理）——都在各自的配置文件里写清楚了原
  因，不是悄悄压掉。

新增22个测试（总共149个，此前127个）。这一节里的每一项检查都先在真实
代码库上本地跑通过，才加进CI，不是先加进CI再等着它报错。

### 最近完成的（稳定性、速度与可观测性，来自同一轮审查）

同一轮审查的第三档：让失败变得可诊断，遇到数据不够的时候拒绝给出一个自
信的结论，而不是新增检查项。完整内容见上文"可靠性：重试、错误分类、拒
绝给出一个站不住脚的结论"，简单说：

- **`_get()` 现在会重试瞬时性HTTP失败**（超时、连接错误、5xx、429），
  带抖动的指数退避；查询字符串也换成了 `urllib.parse.urlencode`，不再
  是裸字符串拼接。
- **`run_scan` 里并发抓取的失败现在会被分类**，不再全部塞进同一句笼统
  的消息——`unscoreable` 的原因现在会说清楚*为什么*（超时/网络错误/数
  据无效），并且会区分"K线抓取失败了"和"抓到了但太薄"这两种情况。
- **`scan --json` 新增 `failure_summary`**——对 `unscoreable` 原因做的
  频次统计，一眼就能看出出了什么问题、有多少个。
- **`recommend` 现在会拒绝给出结论**（明确的 `NO_TRADE`），如果超过
  50%的候选池子完全评估不了，而不是从一小撮不一定有代表性的池子里悄悄
  挑一个"Top pick"。
- **`fetch_stock_tokens()`/`fetch_klines()` 现在做了进程内缓存**——不
  会再出现 `run_scan()` 内部已经抓取过代币列表之后，为了检查持仓又单
  独重新抓一次的情况了。刻意没有把这个扩展到池子状态/APY/TVL上——原因
  见上文"可靠性"。
- **每一个JSON输出现在都共享同一套外壳**（`schema_version`、
  `status`、`run_id`、`as_of`，成功还是出错都一定有）——以前出错路径
  只打印一个裸的 `{"error": ...}`，这些字段一个都没有，成功路径也压
  根没有明确的 `status` 字段。

新增5个测试（总共127个）。做过实测验证（成功抓取、特殊字符的URL编码、
一个真实不可达的host重试后正确失败并给出可诊断的错误；针对缓存这项修
复，还专门统计了一整次 `recommend` 运行里实际发出的HTTP请求数，确认第
二次 `fetch_stock_tokens()` 调用变成了缓存命中，而不是真的又发了一次
请求；针对JSON外壳这项修复，把所有JSON输出点——成功和出错、三个命令全
部覆盖——都直接检查了一遍新字段），也做过两个合成的端到端场景验证
`recommend` 的拒绝阈值（正常情况不受影响，高失败率情况下正确拒绝）。

### 最近完成的（重要逻辑与体验问题，来自同一轮审查）

同一轮审查里的第二档问题——够不上发布阻断项，但确实是这个工具在检查什么
上的真实缺口：

- **`recommend` 不再把仓位读取失败伪装成"没有持仓"。** 之前会话过期、网
  络错误、或响应格式异常都会悄悄退化成 `held=[]`——跟真的没有持仓完全无
  法区分。现在会打印明确的"无法检查你的持仓"提示。
- **`rebalance-check` 现在会评估一个持仓的每一个 `investmentId`，不只是
  第一个**，就连fallback路径（针对扫描页范围之外的持仓）也带上了和
  `scan` 一样的同ticker异常值/协议安全分上下文——之前那条fallback路径两
  个都没传，导致一个本该靠这两项检查被标记的池子可能悄悄溜过去。同一个
  持仓下的多个id现在按最坏情况聚合：任何一个id被标记，整个持仓就算被标
  记；报出来的 `vol_ratio` 是所有能评估的id里最差的那个；评估不了的id
  会被计数并报告出来，不会被悄悄丢掉。
- **V4系列池子现在通过结构化字段 `defiProtocolId` 识别，不再靠显示名字
  匹配。** 专门去检查了一下真实API有没有更结构化的字段（而不是假设没
  有），结果发现了一个真实存在的漏洞：PancakeSwap自己的V4版本叫
  "PancakeSwap Infinity"——这个显示名字里完全没有 "v4"/"V4" 这几个字
  符——旧的名字匹配逻辑会悄悄放过这些池子，让它们绕开硬性拦截。实测确
  认：`defiProtocolId="pancakeswap4"`，发现问题的当时BSC上有3个这样的
  池子在跑。目前都还不是bStock池子，但这个漏洞是真实存在的，不是纸上谈
  兵。
- **稳定币地址列表挪到了 [`stablecoins.json`](stablecoins.json)**，可以
  用 `BSTOCKS_STABLECOIN_CONFIG` 覆盖——见上文"不是所有池子都是稳定币计
  价的"。
- **`--allow-v4` 现在需要带一个理由，不是一个裸标志**——
  `--allow-v4 "已由X审计过"`，原样记录在每个有 `--json` 输出的命令的
  `v4_override_reason` 字段里。覆盖一道"因为工具看不见hook风险才存在"
  的拦截，理应留下"为什么"的记录，而不只是"有人这么做过"。
- **`rebalance-check` 现在会指出一个具体的 `best_alternative`，并计算真
  实的换仓结论**，不再只是对比评级——见上文"rebalance-check：给一个具体
  的替代方案，不只是评级对比"。

新增17个测试（总共122个，此前105个）。每一处修复都对照真实市场数据做过
验证——V4那一项还专门确认了旧逻辑漏掉的那个真实池子。

### 最近完成的（发布前阻断项，来自第二轮外部审查）

第二轮针对代码库本身的跟进审查，标出了四个"投入真实资金之前必须解决"的
阻断项。按审查排定的优先级顺序修复：

- **Windows下 `baw()` 的命令注入风险（本轮最重要的安全修复）。** 之前
  `baw()` 用字符串拼接构造一整条shell命令，再以 `shell=True` 执行——
  `subprocess.list2cmdline` 只做CRT风格的argv引号处理，不会转义shell元字
  符，所以流入某个参数的值（CLI的 `--investmentId` 参数，或者从API响应里
  取出来的id）只要含有 `&`、`|`、`^` 等字符，就有可能跳出预期的命令。现在
  每个参数在能到达命令行之前都会先过一遍shell元字符黑名单校验，`baw` 的
  绝对路径通过 `shutil.which` 解析（不再靠shell的PATH搜索），
  `subprocess.run` 全程用 `shell=False`。实测验证：故意构造的
  `"123 & calc.exe"` payload现在会在触发subprocess之前就被 `ValueError`
  拒绝；非ASCII输出的解码（中文池子名/公司名）跟修复前逐字节完全一致（直
  接对照修复前的代码路径确认过）——没有引入回归。
- **`relative_annualized_volatility` 针对稀疏/未对齐K线做了加固。** 非稳
  定币配对路径此前只按开盘时间对两条独立抓取的K线序列取交集，对交集有多
  稀疏、有多少空档完全没有下限——两条独立抓取的序列完全可能交出一个比任
  何一条单独序列都要稀疏得多的重叠（上线时间不同、链上活跃度不均），悄悄
  低估真实方差的同时，还照样给出一个看起来正常的数字。现在要求至少30根
  对齐K线，*并且*对齐K线覆盖了首尾之间理论满密度跨度的至少80%；任何一项
  不满足就返回 `sigma=None` 并带上具体原因，走进现有的 `unscoreable` 报
  告通道，而不是给出一个看似精确、实则误导的数字。
- **`resolve_pool_stock_and_quote` 里的两资产假设加上了校验。** 之前它
  只挑第一个匹配到的bStock、以及第一个地址不同的代币当作"配对资产"，完全
  没检查一个池子是不是真的正好有2个资产、正好1个bStock——一个3资产的加权
  池，或者一个双bStock池，都会被悄悄当成普通的2资产配对处理，而这个工具
  的 `E[IL] ~ σ²/8` 模型根本不适用于这些情况。现在要求正好2个不同的链上
  地址、正好1个确认过的bStock；其他情况一律返回
  `pair_mode="unsupported"`，带具体原因进入 `unscoreable`，而不是被悄悄
  算错。
- **`net_apy` 改名为 `model_net_apy`，并新增 `MODEL_APY_CAVEAT`**——见上
  文"注意事项"。改名之前专门实测对照了 `defi investment-info` 在100个抽
  样池子上的返回内容，确认那个API里确实没有手续费/激励拆分、没有时间戳、
  也没有锁仓/到期数据——先确认这是一个真实的数据局限，再把它写成文档，而
  不是编一个假的拆分出来。

新增/改写6个测试（总共105个，此前99个）。每一处修复都对照真实的 `baw`
CLI和真实市场数据做过实测验证，不只是跑测试套件。

### 最近完成的（来自一次外部代码审查）

一次针对代码库本身（不只是CLI表现）的只读外部审查，发现了一个真实的正确
性bug和几个设计缺口。按审查建议的顺序修复：

- **Yang-Zhang OHLC波动率估计器**，替换掉稳定币计价池子原来的纯收盘价方
  法——漂移无关，相同样本量下统计效率高约5-14倍，用的是K线数据里本来就
  有的OHLC数据（见上文"核心思路"）。经过检索、扎根于已发表的估计器文献，
  不是单凭记忆实现的。
- **精确的双边界从未离开概率**，用闭式的反射法级数解替换掉保守的并集上
  界近似——上线前先在 `test_riskscreen.py` 里用直接的蒙特卡洛路径模拟做
  了验证（一个算错了的"精确"公式，会比它替换掉的那个诚实的近似还要糟糕）。
- **非稳定币配对的波动率（P0，真实bug，不是简化说明）。** `NVDAB-BNB`/
  `BNB-SPCXB`/`HOODB-BNB` 这类池子之前只用bStock自身的波动率打分，等于悄
  悄把BNB当成完全不动。现在通过 `resolve_pool_stock_and_quote()` +
  `relative_annualized_volatility()` 正确计算——见上文"不是所有池子都是
  稳定币计价的"。
- **明确的 `NO_TRADE` 结论（P0）。** 当没有任何候选能通过
  `passes_trade_gate()`（净收益为正、vol_ratio<1）时，`recommend` 不再打
  印"Top pick"——见上文"每一个结果都是四种结论之一"（后来扩展成了完整的
  ENTER/WATCH/NO_TRADE/UNSCOREABLE结论体系）。
- **`range` 推荐区间的情景压力测试**——用估算σ的1x/1.5x/2x
  （Neutral/Elevated/Stress）重新算，让建议对波动率估计的敏感度看得见，
  而不是隐含在一个数字背后。（完整的历史回测/校准这一轮明确没做——见
  Roadmap。）
- **`--json` 纯JSON契约**（`scan`/`positions`/`rebalance-check`），带
  `as_of`、`elapsed_seconds`、`flagged`、`unscoreable` 字段——之前
  `--json` 会先打印人类可读的表格，调度器没法直接解析stdout。
- **统一的评估路径。** `rebalance-check` 现在内部调用 `run_scan()`，不再
  跑自己那套单独的（之前不一致——完全没传 `peer_apys`/
  `protocol_security_score`）市场对比逻辑。`scan` 和 `rebalance-check` 现
  在结构上不可能对同一个池子得出不同的安全结论。
- **`unscoreable` 报告**——抓取失败、确认不了是bStock、或没有可用K线重叠
  的池子，现在会单独报告，不再悄悄丢掉、跟"检查过没问题"混为一谈。
- **Uniswap V4 默认硬拦截**（`--allow-v4` 可覆盖）——见上文"Uniswap V4
  池子默认硬拦截"。
- **CLI参数校验**——负APY、负的/退化的offset、`--max-pages <= 0` 等现在
  在argparse层就会被拒绝（`_positive_int`/`_nonneg_float`/
  `_apy_fraction`/`_offset_fraction`），不会传进模型内部。
- **仓库基础设施**：`LICENSE`（MIT）、`requirements.txt`、GitHub Actions
  CI流程（`.github/workflows/test.yml`，每次push跑 `py_compile` + pytest）
  ——这些之前都没有。
- **新增30个单元测试**（总共83个），覆盖相对波动率修复、V4拦截、
  `evaluate_pool`、`passes_trade_gate`、CLI校验器。

### 最近完成的（来自一轮 PM/QA 审查）

一轮完整的产品审查加上一轮完整的测试，产出了下面这些，加上"运行状态"里提
到的崩溃/正确性bug修复：

- **`recommend`**——补上了"没有单一入口"这个缺口。一个命令，一句结论：最
  优选择、它的区间建议、对当前持仓的检查，不用再自己判断该跑
  `scan`/`range`/`positions` 里的哪一个。
- **`--capital <金额>`**（`recommend`/`scan`/`range` 都支持）——具体的仓位
  规模建议：预期年化$收益，以及如果存款会超过池子TVL的20%就发出集中度警
  告（`position_sizing_note`）。
- **`--target-offset`**（`range --side sell/buy`）——精确的目标价偏移量，
  不再只能凑预设的±5/10/20/30/50%扫描。
- **可配置的存款前筛查阈值**（`scan` 的 `--max-fee-rate`、`--min-tvl`、
  `--peer-outlier-multiple`、`--min-security-score`）——筛查的严格程度不再
  是写死的。
- **分页**（`scan` 的 `--max-pages`，默认3）——池子发现不再卡在按apy排序的
  前100个；排在第150位、APY较低但波动率更划算的池子现在也能发现了。
- **池子名称匹配更健壮**——预筛选现在按 `-/_` 和空白字符分割（不只是
  `-`），也会匹配裸ticker；一个链上 `assetTokenList` 确认不了是bStock的池
  子现在会被跳过，而不是仍然信任名字匹配的猜测（在做这次拓宽的同时顺手修
  了一个真实存在、虽然罕见的张冠李戴bug）。
- **`rebalance-check --json`**，带每个仓位的 `needs_attention` 字段——为接
  入 `schedule` 技能而做：接一个定时检查，只在真出问题时才通知用户，而不
  是不管有没有变化都发报告。
- **`test_riskscreen.py`**——53个单元测试，覆盖所有纯数学函数
  （`breakeven_volatility`、`vol_richness_ratio`、`no_exit_probability`、
  `pool_risk_flags`……）。用 `pytest test_riskscreen.py` 运行。
- **并发化 `baw`/K线请求**——分页（见上）让 `scan` 串行的逐池子调用成了主
  要耗时来源；具体的 43.7秒 → 10.1秒 实测数据见"性能"一节。
