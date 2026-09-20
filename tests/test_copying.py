"""End-to-end: feed frames -> copies -> settlement -> books, no network."""
import sys, os, json, time, tempfile
d=tempfile.mkdtemp(); os.chdir(d); os.makedirs("data")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crypto import cconfig as C
C.TRADING_ALLOCATION=300.0; C.MODE="paper"
json.dump({"traders":[{"wallet":"0xAAA","name":"alpha"},
                      {"wallet":"0xBBB","name":"beta"}]},
          open("data/crypto_traders.json","w"))
from crypto import run as R, engine as E, settle, feed

now=time.time()
slug_ok=f"btc-updown-5m-{int(now)-60}"
def frame(wallet,name,asset,price,shares,slug,tx,oi=0,side="BUY",ts=None):
    return json.dumps({"topic":"activity","type":"trades","payload":{
        "proxyWallet":wallet,"name":name,"slug":slug,"asset":asset,"side":side,
        "price":price,"size":shares,"timestamp":ts or (now-1),
        "transactionHash":tx,"outcomeIndex":oi,
        "outcome":"Up" if oi==0 else "Down"}})

bot=R.Bot()
print(f"following {len(bot.pinned)} traders, allocation ${C.TRADING_ALLOCATION}\n")

frames=[
 frame("0xAAA","alpha","tokA",0.45,200,slug_ok,"0x1"),          # copy
 frame("0xAAA","alpha","tokA",0.47,200,slug_ok,"0x2"),          # same mkt again
 frame("0xCCC","carl","tokC",0.45,200,slug_ok,"0x3"),           # not ours
 frame("0xBBB","beta","tokB",0.88,200,f"eth-updown-5m-{int(now)}","0x4"), # dear
 frame("0xBBB","beta","tokD",0.45,200,f"sol-updown-15m-{int(now)}","0x5"),# copy
 frame("0xBBB","beta","tokE",0.45,1,f"xrp-updown-5m-{int(now)}","0x6"),   # dust
 frame("0xAAA","alpha","tokF",0.45,200,"mlb-yankees-2026-09-21","0x7"),   # sport
 frame("0xAAA","alpha","tokG",0.45,200,f"btc-updown-5m-{int(now)}","0x8",
       ts=now-60),                                               # stale
]
for f in frames: bot.on_trades(feed.parse(f))

print(f"\ncopied {bot.copies} (expect 2)")
for why,n in sorted(bot.skips.items(), key=lambda kv:-kv[1]):
    print(f"  {n}  {why}")
assert bot.copies==2, bot.copies

print(f"\nequity ${E.equity(bot.s):.2f}  cash ${bot.s['cash']:.2f}  "
      f"deployed ${E.deployed(bot.s):.2f}")
for tok,p in bot.s["positions"].items():
    print(f"  {tok}  {p['trader']:<6} {p['price']*100:.0f}c  ${p['cost']:.2f}  {p['slug'][:30]}")

# settle: tokA wins, tokD loses
settle.winning_asset = lambda s: "tokA" if s==slug_ok else "nope"
for slug in bot.pending: bot.pending[slug]["end"]=now-300
closed=bot.settle_due(now=now)
print(f"\nsettled {closed}")
print(f"  realised {bot.s['realized_pnl']:+.2f}  "
      f"{bot.s['wins']}W/{bot.s['losses']}L  equity ${E.equity(bot.s):.2f}")
drift=E.books_drift(bot.s)
print(f"  books drift {drift:+.4f}")
assert abs(drift)<0.01, drift
bot.persist()
print("\nfiles:", sorted(os.listdir("data")))
log=json.load(open("data/crypto_trades.json"))
print(f"trade log: {len(log)} entries, events: "
      f"{[e['event'] for e in log]}")
print(f"lag recorded on copies: {[e.get('lag_seconds') for e in log if e['event']=='copy']}")
print("\nALL CHECKS PASSED")
