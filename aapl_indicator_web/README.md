# 多周期 K 线与指标系统

当前启用板块为代币化美股、加密货币。Flask 提供本地 SQLite 数据接口，ECharts 显示 OHLC、Smoothed HA、ZigZag、三清线与策略标记。

## 启动

需要 Python 3 和可访问的本地数据库。安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r aapl_indicator_web/requirements.txt
```

先修改 `aapl_indicator_web/config.yaml` 中的数据库路径和服务设置，适配部署机器；当前配置保留原机器路径，不会自动下载数据库。随后启动：

```bash
AAPL_VIS_PYTHON="$PWD/.venv/bin/python" bash aapl_indicator_web/start.sh
```

端口默认 5011。`start.sh` 接受 `AAPL_VIS_HOST`、`AAPL_VIS_PORT`、`AAPL_VIS_PYTHON`；远端 DuckDB 模式需要通过配置指定的环境变量提供 SSH 密码，禁止提交真实凭据。

加密货币历史数据可通过 `backfill_binance_bsc_futures.py` 和 `config.binance_largecap_perpetual.yaml` 采集。其他采集脚本及配置为历史市场适配工具，不代表当前板块已经启用。

## 跨周期点击联动

点击 1m、5m 或 15m 图中的 K 线，其他图按实际时间区间跳转。红色竖虚线定位；映射到低周期时，双虚线及淡红区域覆盖对应 K 线。图下说明具体时间和根数。

- 时间区间为 `[start_ts, end_ts)`，避免跨周期边界重复匹配。
- 保留各图缩放宽度，并使对应区间进入视野。
- 新数据加载后清除旧定位；缩放和显示指标不会修改策略信号。
- 显示参数在 `config.yaml` 的 `indicators.chart_link` 下。

联动逻辑测试（需要 Node.js）：

```bash
node tasks/chart_time_link/test.cjs
```

## 数据及研究边界

本次同步仅包含代码、配置和测试，不包含本地数据库更新、日志、截图或独立回测结果。仓库早期提交可能含历史示例数据库；其存在不保证当前配置引用的数据库齐全。

图表策略标记属于研究用途。历史 ZigZag 存在重画风险，不应将图表回看收益视为已验证可执行收益。回测任务必须通过研究环境的 8899 任务管理器提交，不能以网页标记计算替代正式回测。
