# Shot Detect Algorithm

The algorithm trades on rollbacks after sharp price shots. Shot Detect monitors the trade flow in real time and captures the moment when the price makes a sharp shot and starts to return. Entry happens not during the move itself, but after the rollback reaches the configured range.

The algorithm divides the observation window into small time intervals, tracks price shots and trading activity, and then automatically places an order when the configured conditions are met.

***

### How the algorithm works

1. **Splitting time into intervals** \
   The frame is divided into 3 equal intervals. The observation window is rolling: every \~⅓ of the frame a new interval is added and the oldest one drops off. At any moment the algorithm analyzes a window of 3 intervals. \
   For example, with a frame size of 1 second, each interval lasts \~0.33 seconds.
2. **Collecting data in each frame** \
   In each frame the algorithm analyzes:
   * the minimum and maximum trade price;
   * the number of trades (Bid and Ask);
   * the trade volume (Bid and Ask).
3. **Checking entry conditions** \
   The algorithm requires **3 fully formed intervals**; the trigger fires based on the middle one. This allows the algorithm to determine whether a rollback is happening. \
   The shot direction is determined by the first and middle interval:

   * the midpoint of the middle interval is calculated: (max + min) / 2
   * it is compared with the first price of the first interval
   * if the first price is below the midpoint — the shot is **up**
   * if the first price is above the midpoint — the shot is **down**

   <figure><img src="https://2194691319-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FPMuOulAKIicy77MPPDXF%2Fuploads%2FZ3nYcCrU0eBslOjYpqm4%2Fimage.png?alt=media&amp;token=7b55fa52-d1a8-4a6e-9d81-7464fc6097e8" alt=""><figcaption></figcaption></figure>

   The rollback is measured relative to the size of the entire frame, by comparing the boundaries of the two latest intervals:

   * **Shot Down —** the lower boundary of the middle interval is compared with the lower boundary of the latest interval
   * **Shot Up —** the upper boundary of the middle interval is compared with the upper boundary of the latest interval

   <figure><img src="https://2194691319-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FPMuOulAKIicy77MPPDXF%2Fuploads%2FIFjue6zo98aQzNm90phe%2Fimage.png?alt=media&amp;token=ee16f5a3-e925-4eb9-8ccb-08e114d8db31" alt=""><figcaption></figcaption></figure>

   The algorithm generates a signal if:

   * each interval contains at least one trade;
   * the price shot size in the middle interval falls within the Shot Range;
   * the shot direction matches the Shot Direction parameter;
   * the number of trades in the frame is at least Min Trades Per Frame;
   * the total volume in the frame is at least Min Quote Asset Volume.
4. **Placing the order** \
   When the trigger fires, the algorithm waits for the price to roll back into the Rollback Range. The order placement price depends on the selected order behavior — Follow or Fixed.

   * **Follow mode** — the price is calculated from the actual price (the price at the moment the trigger fires) using Distance Factor and shot depth.
   * **Fixed mode** — the price is calculated from the shot boundaries using Distance Factor.

   Detailed formulas and examples — see the [Distance Factor](#id-5.-distance-factor) parameter description.
5. **Position management** \
   After the order is executed, the take profit and stop loss are activated.

   * **Take profit** — the profit target is calculated from the order execution price using TP Distance Factor and shot depth.
   * **Stop loss** — a classic stop loss as a percent of the order execution price.

   Detailed formulas and examples — see the [TP Distance Factor](#tp-distance-factor) and [Stop Loss](#stop-loss) parameter descriptions.

***

### Main parameters

#### **1. Frame**

<figure><img src="https://2194691319-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FPMuOulAKIicy77MPPDXF%2Fuploads%2FM5pa8mgHtOB5I7Xdpoo7%2Fimage.png?alt=media&amp;token=61b6d69d-8af4-4369-9fc8-03911e9d111d" alt=""><figcaption></figcaption></figure>

Duration of one observation frame in seconds.

The frame is automatically divided into 3 equal intervals. The smaller the frame, the shorter the intervals and the more precise the shot analysis.

**Default value:** 0.3 seconds

***

#### **2. Shot Range**

Range of shot depth values in the middle interval, expressed as a percent.

The trigger fires only if the shot depth in the middle interval falls within the specified range.

**Minimum value:** 0%

**Maximum value:** unlimited

***

#### **3. Min Trades Per Frame**

Minimum number of trades that must occur in each frame.

Frames with fewer trades are excluded from trigger evaluation.

**Default value:** 2

**Minimum value:** 0

**Maximum value:** unlimited

***

#### **4. Min Quote Asset Volume**

Minimum total trade volume in the quote asset across the entire observation window.

Helps filter out signals during low liquidity.

**Default value:** 10,000

**Minimum value:** 0

**Maximum value:** unlimited

***

#### **5. Distance Factor**

Multiplier for calculating the order distance, as a percent of the frame size.

Distance Factor behaves differently depending on the order placement mode (Follow or Fixed).

**Follow mode** — Distance Factor is multiplied by shot depth, the order price is calculated from the actual price:

shot depth:

* Buy: `shot depth = (max − min) / max`
* Sell: `shot depth = (max − min) / min`

orderDistance and orderPrice:

* orderDistance = shot depth × Distance Factor
* Buy: orderPrice = actual price × (1 − orderDistance)
* Sell: orderPrice = actual price × (1 + orderDistance)

where **actual price** is the price at the moment the trigger fires.

<figure><img src="https://2194691319-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FPMuOulAKIicy77MPPDXF%2Fuploads%2F72c8x7T6ih3rXXRbnUwU%2Fimage.png?alt=media&amp;token=684b94c0-45eb-4ba6-a9ae-92b50f18e4db" alt=""><figcaption></figcaption></figure>

**Buy example (Follow):** shot from 100 to 50, actual price = 70, Distance Factor = 80%

* shot depth = (100−50)/100 = 0.5
* orderDistance = 0.5 × 0.8 = 0.4
* orderPrice = 70 × (1 − 0.4) = 42

**Sell example (Follow):** shot from 100 to 150, actual price = 130, Distance Factor = 80%

* shot depth = (150−100)/100 = 0.5
* orderDistance = 0.5 × 0.8 = 0.4
* orderPrice = 130 × (1 + 0.4) = 182

**Fixed mode** — Distance Factor is multiplied by the shot size, the order price is calculated from the shot boundaries:

* Shot Up: orderPrice = lower boundary + (upper − lower) × Distance Factor
* Shot Down: orderPrice = upper boundary − (upper − lower) × Distance Factor

<figure><img src="https://2194691319-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FPMuOulAKIicy77MPPDXF%2Fuploads%2FiwyRJbm1DkDcXYSeGX79%2Fimage.png?alt=media&amp;token=278e3151-54cc-43f1-be8f-50f81ef92c91" alt=""><figcaption></figcaption></figure>

**Shot Up example:** lower boundary = 1, upper = 100, Distance Factor = 80%

* shot size = 100 − 1 = 99
* orderPrice = 1 + 99 × 0.8 = 80

**Shot Down example:** lower boundary = 1, upper = 100, Distance Factor = 80%

* shot size = 100 − 1 = 99
* orderPrice = 100 − 99 × 0.8 = 20.8

**Default value:** 80%

**Minimum value:** 0%

**Maximum value:** unlimited

***

#### **6. Order Lifetime**

Time in seconds the order stays active.

In Follow mode the order tracks the price for this duration; in Fixed mode it stays in place. Cancelled after expiration.

**In Follow mode, the timer resets on every new shot that passes the filters.** From the algorithm's point of view this is a new trigger and a new work cycle, even if the order is already placed and following the price.

**Default value:** 1 second

**Minimum value:** 0 seconds

**Maximum value:** unlimited

***

#### **7. Max Orders**

Maximum number of unfilled orders at any moment.

A new order will not be placed if the number of active orders has reached the specified limit. Open positions are not counted.

**Default value:** 1

**Minimum value:** 0

**Maximum value:** unlimited

***

#### **8. Order Frequency**

Frequency of placing new orders in seconds while the trigger is active.

**Default value:** 0.1 seconds

**Minimum value:** 0 seconds

**Maximum value:** unlimited

***

#### **9. Rollback Range**

Allowed rollback range from the maximum price movement of the shot, set as a percent of the frame size.

The order is placed only when the price rollback falls within the specified range.

* 0% — no rollback
* 100% — full return to the starting point of the move

**Default value:** from 0% to 80%

***

#### **10. Shot Direction**

Direction of price movement the algorithm should detect:

* **Up** — sharp price rise
* **Down** — sharp price drop

***

### Order Behavior

Defines how the placed order behaves until it is executed.

#### **Follow mode**

The order follows the price, keeping the orderDistance, until Order Lifetime expires.

A buffer is created around the current price. While the price stays inside the corridor — the order does not move.

#### **Order Distance Auto Adjust**

When enabled, a new order is placed with the orderDistance recalculated for the size of the most recent shot.

**How it works:** the first order is filled (for example, calculated with orderDistance 1.2%), then a stronger new shot arrives (1.8%). The new order is placed with orderDistance 1.8%, adjusted to the size of this shot.

**Buffer Type:**

* **Static** — fixed buffer size, calculated as a percent of the price (for example, 0.4%)
* **Dynamic** — buffer size adapts to the frame size, calculated as a percent of the shot itself (for example, 15%)

<figure><img src="https://2194691319-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FPMuOulAKIicy77MPPDXF%2Fuploads%2FDF4bCZDbfD6NwOFbAg5P%2Fimage.png?alt=media&amp;token=f07864f2-8e8f-4041-b893-311378d9d1f5" alt=""><figcaption></figcaption></figure>

**Buffer** — the width of the corridor in percent within which the price can fluctuate without moving the order.

#### **Follow Price Delay**

Number of seconds the order will not move with the price after crossing the upper buffer boundary (for Buy) or the lower buffer boundary (for Sell).

**How it works:**

1. The price crosses the upper buffer boundary (Buy) or the lower buffer boundary (Sell) — the timer starts
2. For N seconds the order does not move with the price, even if it keeps going further
3. If the price returns to the buffer within the delay — the order does not move
4. If the price crosses the buffer in the opposite direction — the order is repositioned immediately without delay

**Why it's needed:**

* Protects against frequent repositioning during volatility
* Reduces the load on the exchange API

#### **Replace Delay**

Number of seconds the order will not move away from the price after it crosses the lower buffer boundary (for Buy) or the upper buffer boundary (for Sell).

**How it works:**

1. The price crosses the lower buffer boundary (Buy) or the upper buffer boundary (Sell) — the timer starts
2. For N seconds the order does not move away, even if the price keeps approaching
3. If the price returns to the buffer within the delay — the order does not move
4. If the price crosses the buffer in the opposite direction — the order is repositioned immediately without delay

**Why it's needed:**

* Protects against triggering during rapid moves without a subsequent rollback
* Reduces the number of unnecessary order placements

***

**Fixed mode**

The order is placed once at a fixed price and does not move. The price is calculated from the shot boundaries using Distance Factor — see the [Distance Factor](#id-5.-distance-factor) parameter description for detailed formulas and examples.

If the order is not filled within Order Lifetime — it is cancelled automatically.

***

### Profit and risk management

#### **TP Distance Factor**

Multiplier for calculating the profit target from the order execution price, as a percent of the frame size.

TP Distance Factor is multiplied by shot depth — the result is the final distance to the target (tpDistance).

**Formulas:**

shot depth:

* Buy: `shot depth = (max − min) / max`
* Sell: `shot depth = (max − min) / min`

tpDistance and tpPrice:

* tpDistance = shot depth × TP Distance Factor
* Buy: tpPrice = execution price × (1 + tpDistance)
* Sell: tpPrice = execution price × (1 − tpDistance)

where **execution price** is the order execution price.

<figure><img src="https://2194691319-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FPMuOulAKIicy77MPPDXF%2Fuploads%2FwQpDbWU51LXYwOM61uWE%2Fimage.png?alt=media&amp;token=44e32663-ed52-4a25-8d95-43600037906d" alt=""><figcaption></figcaption></figure>

**Buy example:** shot from 100 to 50, execution price = 42, TP Distance Factor = 90%

* shot depth = (100−50)/100 = 0.5
* tpDistance = 0.5 × 0.9 = 0.45
* tpPrice = 42 × (1 + 0.45) = 60.9

**Sell example:** shot from 100 to 150, execution price = 182, TP Distance Factor = 90%

* shot depth = (150−100)/100 = 0.5
* tpDistance = 0.5 × 0.9 = 0.45
* tpPrice = 182 × (1 − 0.45) = 100.1

**Execution type:**

* **Market** — close the position with a market order
* **Limit** — close the position with a limit order

***

#### **Stop Loss**

Classic stop loss as a percent of the order execution price.

**Formulas:**

* Buy: slPrice = execution price × (1 − Stop Loss)
* Sell: slPrice = execution price × (1 + Stop Loss)

where **execution price** is the order execution price.

**Buy example:** execution price = 42, Stop Loss = 2%

* slPrice = 42 × (1 − 0.02) = 41.16

**Sell example:** execution price = 182, Stop Loss = 2%

* slPrice = 182 × (1 + 0.02) = 185.64

**Execution type:**

* **Market** — close the position with a market order
* **Limit** — close the position with a limit order

***

### Contacts and support

If you have any questions about configuring the algorithm or want to suggest improvements, please contact our [support team](https://t.me/moontrader_support).
