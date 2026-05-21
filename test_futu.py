from futu import *
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 连接本地 OpenD
quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)

# 获取美股股票列表（测试用）
ret, data = quote_ctx.get_stock_basicinfo(
    market=Market.US,
    stock_type=SecurityType.STOCK
)

if ret == RET_OK:
    print("连接成功！")
    print(data.head())
else:
    print("连接失败:", data)

quote_ctx.close()

