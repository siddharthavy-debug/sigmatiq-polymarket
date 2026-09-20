import sys, time, tempfile, os
d=tempfile.mkdtemp(); os.chdir(d); os.makedirs("data")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crypto import engine as E, cconfig as C

C.TRADING_ALLOCATION=300.0
s=E.new_state()
now=time.time()
pinned={"0xgood":{"name":"goodguy"}}

def t(**kw):
    base=dict(wallet="0xGOOD", side="BUY", horizon=300, ts=now-1,
              shares=100, price=0.40, asset="tok1", slug="btc-updown-5m-1",
              coin="btc", outcome="Up")
    base.update(kw); return base

ok=0; fail=0
def check(label, trade, expect, state=s):
    global ok,fail
    a,why,det = E.consider(state, trade, pinned, now=now)
    good = (a==expect)
    print(f"  {'OK ' if good else 'FAIL'} {label:<44} -> {a:<5} {why}")
    if good: ok+=1
    else: fail+=1
    return det

print("REJECTIONS")
check("not our trader", t(wallet="0xstranger"), "skip")
check("they sold", t(side="SELL"), "skip")
check("4h market (too long)", t(horizon=14400), "skip")
check("1m market (too short)", t(horizon=60), "skip")
check("stale signal 30s", t(ts=now-30), "skip")
check("their trade under $1", t(shares=1, price=0.40), "skip")
check("price 80c", t(price=0.80, shares=100), "skip")
check("price 8c", t(price=0.08, shares=400), "skip")
check("price 35c (below the band)", t(price=0.35, shares=200), "skip")

print("\nACCEPT + SIZING")
det=check("clean signal", t(), "copy")
print(f"     stake ${det['size']} on ${E.equity(s):.0f} equity "
      f"= {det['size']/E.equity(s)*100:.1f}%  (want 2.0%)")
assert abs(det['size']-6.0)<0.01, det
E.open_position(s, t(), det, now=now)

print("\nAFTER ONE COPY")
check("same trader, same market again", t(), "skip")
check("same market, different trader",
      t(wallet="0xgood2", slug="btc-updown-5m-1"), "skip")   # not pinned
check("same token via another slug", t(slug="btc-updown-5m-2"), "skip")

print("\nDEPLOYMENT CAP (20% of $300 = $60)")
s2=E.new_state()
for i in range(20):
    tr=t(asset=f"tok{i}", slug=f"btc-updown-5m-{i}", wallet="0xgood")
    a,why,det=E.consider(s2,tr,pinned,now=now)
    if a=="copy": E.open_position(s2,tr,det,now=now)
    else:
        print(f"  stopped after {len(s2['positions'])} positions, "
              f"${E.deployed(s2):.2f} deployed -> {why}")
        break
assert E.deployed(s2) <= 60.01, E.deployed(s2)
print(f"  OK  deployed ${E.deployed(s2):.2f} of $60 allowed")

print("\nDAILY STOP (6%)")
s3=E.new_state()
s3["day_start_equity"]=300.0
s3["cash"]=300.0-19.0            # down $19 = 6.3%
a,why,det=E.consider(s3,t(),pinned,now=now)
print(f"  down {E.day_loss_pct(s3)*100:.1f}% -> {a}: {why}")
assert a=="skip"
ok+=1

print("\nSETTLEMENT + COMPOUNDING")
s4=E.new_state()
tr=t(); a,why,det=E.consider(s4,tr,pinned,now=now); E.open_position(s4,tr,det,now=now)
print(f"  bought ${det['size']} at {det['price']*100:.0f}c "
      f"= {s4['positions']['tok1']['shares']:.2f} shares")
E.settle_position(s4,"tok1",won=True,now=now)
print(f"  won -> cash ${s4['cash']:.2f}, realised {s4['realized_pnl']:+.2f}, "
      f"equity ${E.equity(s4):.2f}")
assert abs(E.books_drift(s4))<0.01, E.books_drift(s4)
print(f"  books drift {E.books_drift(s4):+.4f}  (must be 0)")
print(f"  next stake ${E.stake(s4):.2f}  (was $6.00 — compounds)")
assert E.stake(s4) > 6.0

print(f"\n{ok} checks passed, {fail} failed")
