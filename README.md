# A股智能轮动选股 · GitHub Actions 数据端

自动采集A股日K、计算MA5/10/20、MACD、MA20偏离率、成交量比、三日主力资金净流入，生成`docs/data.json`供Netlify网页读取。

## 启动
1. 将本项目文件上传到GitHub新建的仓库 `ashare-quant-workbench`（包括 `.github/workflows/refresh.yml`）。
2. 在仓库 Settings → Actions → General 中，确认允许 Actions 运行；Workflow permissions 选择 **Read and write permissions**（以便自动提交 JSON）。
3. 打开 Actions → A-share rotation refresh → Run workflow，首次手动运行并查看日志。
4. 成功后，访问 `https://raw.githubusercontent.com/<你的用户名>/ashare-quant-workbench/main/docs/data.json` 验证数据。
5. 建议通过GitHub Pages发布 `docs` 目录以获得可被网页读取的CORS接口：Settings → Pages → Deploy from a branch → main → /docs。数据URL：`https://<用户名>.github.io/ashare-quant-workbench/data.json`。
6. 将Netlify网页的数据请求改为上述GitHub Pages JSON URL；不要用文件上传CSV代替自动数据源。

## 定时
Actions cron使用UTC：`0 1,3,6,7 * * 1-5` = 北京时间周一至周五09:00、11:00、14:00、15:00。GitHub可能延迟触发；交易日和节假日需按行情实际日期判断。09:00只能使用上一交易日收盘K线，11:00/14:00此数据端也只提供最新可获取的**日K数据**，不承诺实时盘中价格或未完成日K。15:00后数据源可能延迟。

## 重要限制
- 默认观察池是12只示例候选股，**不是全市场行业轮动**；未来应增加行业涨幅和资金流排名的候选池发现程序。
- 免费公开数据源可能限流、失效或延迟；无法获取资金流则返回`unavailable`，不会虚构。
- 如果全体股票采集失败，不覆盖上一份有效数据。
- `buy_trigger`为突破/均线条件触发价；`stop_reference`为参考止损位，均不是确定成交价。
- 日K计算使用前复权行情，价格口径与盘中未复权报价可能不同。
- GitHub Pages公开展示数据，勿上传账户、持仓或个人信息。
