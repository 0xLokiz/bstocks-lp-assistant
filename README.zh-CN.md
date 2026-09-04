# bStocks LP Assistant

[English](README.md) | [中文](README.zh-CN.md)

> **不构成投资建议，请自行做好研究（DYOR）。** 这个工具给出的所有数字
> 都来自一个自动化模型，不是人工审核，而且代码本身是用AI辅助构建和维
> 护的——它是会犯错的。每个命令的输出末尾都会带同样的提醒。请把这里的
> 每一个数字都当成一个需要自己独立验证的模型估计，绝不要当成可以直接
> 照做的事实——尤其是在真的要存入资金之前。

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
E[IL] ≈ σ² / 8      （年化，满区间 / V2风格流动性——只在这个值 < 1.0 时才有效）
```

其中 `σ` 是代币的年化波动率，用链上K线历史估算。这只是小`σ`情况下的
近似，`σ = √8 ≈ 283%` 年化以上就会发散——超过这个点，模型给出的是
**N/A**，而不是一个数字（封顶到某个数字看起来挺安全，但近似公式已经
离开了它本该适用的范围，封顶数字照样是假精度——这是拿一个真实存在、
波动率就是这么大的池子验证过的，不是编出来的极端情况）。集中区间的
数字没有这个问题——它们锚定的是精确的边界IL，而不是这个近似公式，所
以在任何波动率下都有效。

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
| apy 超过同ticker中位数的5倍，且至少有3个同ticker peer | 收益是真的吗？ | **通用情形**——不管是什么机制造成的，已知的还是未知的 |
| `investable = false` | 安全吗？ | 已下架，无法新存款 |
| 协议 `securityScore` < 50 | 安全吗？ | 明显不靠谱的协议（较弱的下限，见下文） |
| 协议是V4系列（`defiProtocolId`） | 安全吗？ | **默认硬性拦截**——见下方"Uniswap V4 池子默认硬拦截" |

**中位数检查需要足够多的对照样本才有意义。** `statistics.median` 算一个
只有1个元素的列表，结果就是那一个元素本身——所以只有1个同ticker peer时，
"是中位数的N倍"其实就是"是另一个池子apy的N倍"，你根本分不清这两个池子里
到底谁才是真正的异常值，而且那唯一的peer本身也可能很吵。这是实测出来的，
不是假设：一个 GMEB-USDT（PancakeSwap V3）池子被标记为"是中位数(84.7%)
的6.0倍"，参照的就是它唯一的peer（一个 Uniswap V4 版本的 GMEB-USDT 池
子）——几分钟后直接核实，这个peer自己的apy已经从84.7%变成了123.20%，
说明这个flag依赖的"中位数"本身就是噪声，不是稳定的基准；而被标记的这个
池子的 `feeRate`（0.25%，标准PancakeSwap费率档位）、TVL（$245K）、也没
有挂任何激励代币——一个能说明数据真的有问题的破绽都没查到。修复方式是加
了 `MIN_PEER_SAMPLE_SIZE = 3`（`scan` 上对应 `--min-peer-sample`）：只有
同ticker至少存在这么多个其他池子时，这个检查才生效。实测确认这个改动没
有削弱真正的异常检测——QQQB-BNB（QQQ这个ticker下有3个以上的peer）依然
被正确标记，而 GMEB-USDT，以及另外两个之前也是只有1个同ticker peer的池
子（HOODB-BNB、SNDKB-USDT），现在都正确地不再被这条peer-outlier规则单
独排除（HOODB-BNB本身因为TVL太低这条独立信号，还是会被排除）。

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
hook 会按三种命名模型之一逐笔重新定价手续费（Calendar——感知交易时段/隔
夜/周末/假日状态；Flat base——固定基准；Directional——响应订单流方向）。
它自己的文档（fables.fi/docs/swap-fee）证实即便是 keeper 驱动的临时调整
也是链上有边界的——最多在模型费率基础上打5折、最长72小时有效期、加上写
死在合约字节码里的绝对费率上限；同时把显示的费率描述为"最新的合约读数，
不是能约束后续交易的报价"。这种池子的 `feeRate` 快照是一个实时的瞬时数
字——我们的静态费率年化方法，不管这个hook本身是否合法，本来就是用错了工
具。这个 flag 的含义是"我们没法给这个算出有意义的apy"，不是指控。

**值得说清楚的局限**：`securityScore` 来自 `defi protocol-info`，是按*协
议*算的，不是按池子或按hook算的——Uniswap V3 和 V4 的分数都是95.18，因为
是同一个组织。这个信号在QQQB那个案例上完全没有预警。它是针对明显不靠谱协
议的一道下限，不是hook审计——真正更深层的V4-hook安全项还在Roadmap里，这
个信号替代不了它。这个默认拦截也不是对着假想敌过度谨慎：Fables 自己的安
全页面（fables.fi/docs/security）明确写着目前没有发布任何针对Fables本身
的审计报告——而这是一个复杂到能跑三种有边界费率模型的协议。作为一个真实存
在、正在运行的V4 hook协议，从来就不能证明某一个具体的hook是安全、可以盲
目信任的。

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
  （`--json` 里对应 `model_apy_caveat` 字段）。独立的佐证：
  [Fables](https://www.fables.fi/docs/methodology)——一个跟本项目毫无关系
  的、正在运行的 Uniswap v4 hook 交易所——自己的文档也是用同样的方式给池
  子的头部APR年化（24小时手续费窗口除以当前TVL），它自己的文档还提醒这个
  数字不是预测，也没有扣掉相对于持有的价值差异——这正是 `model_net_apy`
  要从平台原始 `apy` 里减去估算IL的原因，而不是直接把原始数字端出来。
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
  [--min-peer-sample 3] [--min-security-score 50]
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
test_riskscreen_integration.py` 总共跑165个测试。`test_riskscreen.py`
（135个）覆盖纯数学/纯逻辑函数，完全不涉及I/O。
`test_riskscreen_integration.py`（30个）端到端地跑
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
（`python riskscreen.py scan --top 8 --with-range`，BSC，2026-09-03）：

```
pool                ticker  protocol              apy      vol  grade best +/-%  range-net  confidence           tvl  verdict
GMEB-USDT           GME     PancakeSwap V3    784.16%   40.46%   Rich       90%    900.37%        High       132,769  ENTER
USDT-MRNAB          MRNA    PancakeSwap V3    802.94%  158.96%   Rich      full    771.36%        High       133,819  ENTER
NVDAB-BNB           NVDA    PancakeSwap V3    385.75%   43.87%   Rich       90%    425.66%        High        63,226  ENTER  [non-stablecoin pair]
QQQB-BNB            QQQ     PancakeSwap V3    228.39%   30.08%   Rich       50%    430.19%        High        59,824  ENTER  [non-stablecoin pair]
NVDAB-USDT          NVDA    PancakeSwap V3    223.55%   50.12%   Rich       90%    227.91%    Moderate     2,064,242  ENTER
NVDAB-USDT          NVDA    PancakeSwap V3    213.27%   50.12%   Rich       90%    217.24%    Moderate       467,527  ENTER
USDT-SPCXB          SPCX    PancakeSwap V3    189.34%  100.60%   Rich      full    176.69%        High       308,724  ENTER
HOODB-BNB           HOOD    PancakeSwap V3    166.57%   69.80%   Rich      full    160.48%        High        17,838  ENTER  [non-stablecoin pair]

5 pool(s) excluded from ranking -- pre-deposit screen flagged, e.g.:
  GMEB-USDT (Uniswap V4): V4-generation pools can carry an arbitrary custom
  hook with unaudited logic -- blocked by default (--allow-v4 to override)
  BNB-SPCXB (PancakeSwap V3): apy is 5.6x the median (106.4%) of other pools
  on the same token -- outlier, treat as unverified until explained
```

这8个上榜的池子全部评级为 **Rich**，也都通过了 **ENTER**——高得吓人的APY
确实反映了相对于已实现波动率的可观溢价，不只是数字看起来很大而已。
`protocol` 列能看出每一个上榜池子都已经通过了V4硬拦截（见下文）；被排除的
5个池子说明合理性过滤器在正常工作，不是排名漏掉了它们——其中就包括
GMEB-USDT 在 Uniswap V4 上的姊妹池，跟上面排第一的 PancakeSwap V3 池子是
同一个标的，但因为合约风险被排除在外。

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
  [Fables](https://www.fables.fi) 已经在 Robinhood Chain 上线（chain ID
  4663，来自 fables.fi/docs/addresses），交易代币化股票（NVDA/USDG、
  TSLA/USDG、AAPL/USDG、SPY/USDG……），背后有一个公开的
  Blockscout 浏览器 `robinhoodchain.blockscout.com`——Blockscout实例通常都
  会暴露 REST/GraphQL API，这是一条可行的近期路径，可以像
  `fetch_stock_tokens`/`fetch_klines` 对接Binance的bapi那样去拉可比的市场
  /池子数据。如果那边的数据确实可比地拿得到，把 `stocks`/`vol`/`scan` 扩
  展过去，就能把这个工具从"只看币安内部"升级成"跨平台比价"——对回答"同一
  个标的，哪个平台的LP更划算"这种问题真正有用，而不只是平台内部选哪个池子。
- **分时段波动率。** Fables 记录在案的费率模型（fables.fi/docs/swap-fee）
  里有一个"Calendar"模型，之所以在交易时段/隔夜/周末/假期分别定价，正是
  因为代币化股票的交易行为在这些时间窗口确实不一样（标的资产只有在纽交所
  /纳斯达克开盘时间才有真实的做市）——这是三种命名模型之一，另外两种是
  Flat base（固定基准）和 Directional（响应订单流方向）。
  `annualized_volatility()` 目前把所有K线当成一个同质序列处理；把已实现波
  动率拆成"正常交易时段" vs "盘后"两段（用
  `binance-tokenized-securities-info` 的市场状态API给每根K线打标签），大概
  率能让 `vol_ratio` 和区间建议更精准——这正是Fables专门做一整套费率模型的
  原因。
- **单边区间的真实成交价模型**——现在 `--side sell`/`buy` 区间还在借用
  IL-vs持有公式当成本代理；真正的问题（"相比边界处的普通限价单，我实际的
  平均成交价是多少"）需要自己的模型，不是借来的。
- **波动率不确定性感知评分。** `vol_ratio` 目前仍然用的是已实现波动率的
  点估计（Yang-Zhang，不只是纯收盘价，但无论如何还是一个点估计）；K线
  历史短的话这个估计会很吵。在计算 `vol_ratio` 之
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
  一个上去，而且跟上面的历史校准这一项关系密切（一个快照存储基本上就
  是纸面交易harness需要的大部分东西，可以拿来回放）。
