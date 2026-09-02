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

## 没有候选池子也可能意味着 NO_TRADE

通过存款前筛查（见下文）只是回答了"这个池子安不安全、合不合理，值不值得
考虑"——不代表"这就值得做"。`recommend` 现在会用 `passes_trade_gate()` 给
"Top pick"设一道门槛：`model_net_apy` 必须为正，**并且** `vol_ratio < 1`（不能
是Cheap档）。改之前，即便所有候选池子扣完IL都是负收益，或者评级都是
Cheap，`recommend` 依然会打印一个"Top pick"——这读起来像是一种从没打算做
出的背书。当没有任何候选能同时满足这两条门槛时，`recommend` 会打印一个明
确的 `NO_TRADE` 结论，说明具体原因，并且只把最接近门槛的候选列出来作参考，
明确标注"这不是推荐"。

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
| 协议名包含"V4" | 安全吗？ | **默认硬性拦截**——见下方"Uniswap V4 池子默认硬拦截" |

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
的池子，可以传 `--allow-v4`；更深层的修复（真正的hook审计，等有这个数据
之后）还在Roadmap里。

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
`scan` 是vol_ratio对比图），不只是一张原始表格——见 `SKILL.md` →
"Visualizing results"。示例，来自真实的 NVDAB-USDT 数据：

*（图表：横轴 p_active，纵轴 model_net_apy，每个候选区间一个点，推荐的±50%区间
高亮显示——在 Claude 会话里实际使用这个 skill 可以看到渲染出来的版本）*

## 使用方式

```bash
# --- 单一入口：一句话结论（需要已登录的 `baw` 会话）---
python riskscreen.py recommend [--capital 10000]

# --- 市场数据（公开API，无需鉴权）---
python riskscreen.py stocks --limit 20 --type 1
python riskscreen.py vol --ticker TSLA --days 30 --apy 0.30

# --- 排名/建议（需要已登录的 `baw` 会话）---
python riskscreen.py scan --top 15 [--with-range] [--json] [--capital 10000] [--allow-v4]
  [--max-pages 3] [--max-fee-rate 0.05] [--min-tvl 5000] [--peer-outlier-multiple 5]
python riskscreen.py range --investmentId <id> [--side straddle|sell|buy] [--allow-v4]
  [--target-offset 0.15] [--band-width 0.10] [--capital 10000]
python riskscreen.py range --ticker TSLA --apy 0.30 --side sell   # 不查真实池子也行

# --- 持仓 + 再平衡（需要已登录的 `baw` 会话）---
python riskscreen.py positions [--refresh] [--json]
python riskscreen.py rebalance-check [--json] [--max-pages 3] [--allow-v4]   # --json 用于定时监控
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

**测试**：`pip install -r requirements.txt && pytest test_riskscreen.py`
跑114个单元测试，覆盖所有纯数学/纯逻辑函数（不需要网络/baw）——CI
（`.github/workflows/test.yml`）在每次push时会跑这个加上 `py_compile`。每
个命令的真实数据冒烟测试记录在下面的"运行状态"里。

**执行环节故意不在这个脚本的范围内。** `rebalance-check` 只打印报告，不会
挪动任何东西。要对某个建议采取行动，走 `binance-agentic-wallet` 的 `defi
preview` → 跟用户确认 → `defi deposit` / `defi lp-add` / `defi redeem` /
`defi lp-remove`，用这个工具打印出来的 `investmentId` / 代币地址。具体流程
见 `SKILL.md` → "Executing a recommendation"。

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
- **自动化公司行为拦截。** 现在"查一下有没有即将发生的公司行为"这一步只是
  `SKILL.md` 里的人工提醒；应该直接调用
  `binance-tokenized-securities-info` 的资产市场状态API，在财报/分红/拆股
  日期落在建议的时间窗口内时，自动放宽有效波动率估计或者直接标红这个池子。

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

新增9个测试（总共114个，此前105个）。每一处修复都对照真实市场数据做过
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
  印"Top pick"——见上文"没有候选池子也可能意味着NO_TRADE"。
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
