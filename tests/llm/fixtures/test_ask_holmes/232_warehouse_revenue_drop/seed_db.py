"""Seed a SQLite database with e-commerce warehouse data for revenue drop analysis.

Ground truth: Net revenue dropped significantly in March vs February.
Root causes (a human analyst would find ALL of these):
1. Returns spiked in March - product_id=7 "UltraWidget Pro" had a defective batch recall,
   generating massive returns (visible only in the returns table)
2. A stealth price cut happened mid-March on the top seller (product_id=1 "CoreWidget Standard") -
   price dropped from $49.99 to $29.99 on March 15, visible only in pricing_history table
3. Heavy promotional discounting via "SPRING_CLEAR" promo (30% off Widgets) hit the
   highest-volume category hard - visible in order_lines.promo_code + promotions table

The twist: GROSS revenue (order_lines.line_total) actually looks similar month-over-month
because March has more days. The LLM must check RETURNS to see the net picture,
check PRICING_HISTORY to find the price cut, and check PROMOTIONS to see the discount impact.
A naive analysis that only looks at order_lines will miss the full story.
"""

import sqlite3
import os
import random

DB_PATH = "/tmp/holmesgpt_eval_232.db"


def seed():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # -- PRODUCTS --
    c.execute("""
    CREATE TABLE products (
        product_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        supplier_id INTEGER,
        created_at TEXT
    )""")

    products = [
        (1, "CoreWidget Standard", "Widgets", 1, "2024-01-15"),
        (2, "CoreWidget Deluxe", "Widgets", 1, "2024-01-15"),
        (3, "MegaGadget Basic", "Gadgets", 2, "2024-02-01"),
        (4, "MegaGadget Plus", "Gadgets", 2, "2024-02-01"),
        (5, "NanoSensor Alpha", "Sensors", 3, "2024-03-01"),
        (6, "NanoSensor Beta", "Sensors", 3, "2024-03-01"),
        (7, "UltraWidget Pro", "Widgets", 1, "2024-01-20"),
        (8, "PowerGadget X", "Gadgets", 2, "2024-04-01"),
        (9, "SmartSensor Gamma", "Sensors", 3, "2024-05-01"),
        (10, "BasicWidget Lite", "Widgets", 4, "2024-06-01"),
    ]
    c.executemany("INSERT INTO products VALUES (?,?,?,?,?)", products)

    # -- CUSTOMERS --
    c.execute("""
    CREATE TABLE customers (
        customer_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        segment TEXT NOT NULL,
        region TEXT NOT NULL,
        signup_date TEXT
    )""")

    segments = ["Enterprise", "SMB", "Consumer"]
    regions = ["North", "South", "East", "West"]
    for i in range(1, 51):
        c.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?)",
            (i, f"Customer_{i:03d}", segments[i % 3], regions[i % 4],
             f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"),
        )

    # -- PRICING_HISTORY (the hidden trap - prices change over time) --
    # The order_lines table stores the actual unit_price at time of sale,
    # but pricing_history shows the REASON for the price change.
    c.execute("""
    CREATE TABLE pricing_history (
        pricing_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        effective_date TEXT NOT NULL,
        end_date TEXT,
        change_reason TEXT,
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""")

    pricing = [
        # Product 1 - TOP SELLER - big price drop mid-March
        (1, 49.99, "2024-01-01", "2025-03-14", "initial_price"),
        (1, 29.99, "2025-03-15", None, "competitive_pressure"),
        # All others stable
        (2, 89.99, "2024-01-01", None, "initial_price"),
        (3, 29.99, "2024-01-01", None, "initial_price"),
        (4, 59.99, "2024-01-01", None, "initial_price"),
        (5, 149.99, "2024-01-01", None, "initial_price"),
        (6, 179.99, "2024-01-01", None, "initial_price"),
        (7, 199.99, "2024-01-01", None, "initial_price"),
        (8, 39.99, "2024-01-01", None, "initial_price"),
        (9, 119.99, "2024-01-01", None, "initial_price"),
        (10, 19.99, "2024-01-01", None, "initial_price"),
    ]
    c.executemany(
        "INSERT INTO pricing_history (product_id, unit_price, effective_date, end_date, change_reason) VALUES (?,?,?,?,?)",
        pricing,
    )

    # -- PROMOTIONS --
    c.execute("""
    CREATE TABLE promotions (
        promo_id INTEGER PRIMARY KEY AUTOINCREMENT,
        promo_code TEXT NOT NULL,
        description TEXT,
        discount_pct REAL NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        applicable_category TEXT
    )""")

    promos = [
        ("WINTER_SALE", "Winter clearance", 0.10, "2025-01-01", "2025-01-31", None),
        ("VAL_DAY", "Valentine special", 0.15, "2025-02-10", "2025-02-16", "Gadgets"),
        ("SPRING_CLEAR", "Spring clearance blowout", 0.30, "2025-03-01", "2025-03-31", "Widgets"),
        ("SPRING_LITE", "Spring lite discount", 0.05, "2025-03-01", "2025-03-31", "Sensors"),
    ]
    c.executemany(
        "INSERT INTO promotions (promo_code, description, discount_pct, start_date, end_date, applicable_category) VALUES (?,?,?,?,?,?)",
        promos,
    )

    # -- ORDERS --
    c.execute("""
    CREATE TABLE orders (
        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        order_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed',
        FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
    )""")

    # -- ORDER_LINES --
    c.execute("""
    CREATE TABLE order_lines (
        line_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        discount_pct REAL NOT NULL DEFAULT 0,
        line_total REAL NOT NULL,
        promo_code TEXT,
        FOREIGN KEY (order_id) REFERENCES orders(order_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""")

    # -- RETURNS --
    c.execute("""
    CREATE TABLE returns (
        return_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        line_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity_returned INTEGER NOT NULL,
        return_amount REAL NOT NULL,
        return_date TEXT NOT NULL,
        return_reason TEXT,
        FOREIGN KEY (order_id) REFERENCES orders(order_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""")

    random.seed(42)

    cat_map = {
        1: "Widgets", 2: "Widgets", 3: "Gadgets", 4: "Gadgets",
        5: "Sensors", 6: "Sensors", 7: "Widgets", 8: "Gadgets",
        9: "Sensors", 10: "Widgets",
    }
    base_price = {
        1: 49.99, 2: 89.99, 3: 29.99, 4: 59.99, 5: 149.99,
        6: 179.99, 7: 199.99, 8: 39.99, 9: 119.99, 10: 19.99,
    }

    # Weight product 1 heavily - it's the top seller (~30% of lines)
    product_weights = [1, 1, 1, 1, 1, 2, 2, 3, 4, 5, 6, 7, 7, 8, 9, 10]

    order_id_counter = 0
    line_id_counter = 0
    returns_data = []

    for month in [2, 3]:
        days_in_month = 28 if month == 2 else 31
        # Same daily order rate to make comparison fair
        daily_orders = 8

        for day in range(1, days_in_month + 1):
            date_str = f"2025-{month:02d}-{day:02d}"

            n_orders = daily_orders + random.randint(-1, 1)

            for _ in range(n_orders):
                order_id_counter += 1
                cust_id = random.randint(1, 50)
                c.execute(
                    "INSERT INTO orders VALUES (?,?,?,?)",
                    (order_id_counter, cust_id, date_str, "completed"),
                )

                n_lines = random.randint(1, 4)
                for _ in range(n_lines):
                    line_id_counter += 1
                    prod_id = random.choice(product_weights)
                    qty = random.randint(1, 6)

                    # Price: product 1 drops on March 15
                    if prod_id == 1 and month == 3 and day >= 15:
                        price = 29.99
                    else:
                        price = base_price[prod_id]

                    # Discounts
                    discount = 0.0
                    promo = None
                    if month == 2:
                        # Light Feb discounts
                        if prod_id in [3, 4, 8] and 10 <= day <= 16 and random.random() < 0.2:
                            discount = 0.15
                            promo = "VAL_DAY"
                    elif month == 3:
                        # Heavy March discounts on Widgets
                        if cat_map[prod_id] == "Widgets" and random.random() < 0.65:
                            discount = 0.30
                            promo = "SPRING_CLEAR"
                        elif cat_map[prod_id] == "Sensors" and random.random() < 0.25:
                            discount = 0.05
                            promo = "SPRING_LITE"

                    line_total = round(qty * price * (1 - discount), 2)
                    c.execute(
                        "INSERT INTO order_lines VALUES (?,?,?,?,?,?,?,?)",
                        (line_id_counter, order_id_counter, prod_id, qty, price, discount, line_total, promo),
                    )

                    # Returns
                    if month == 3 and prod_id == 7:
                        # Defective batch - 75% of UltraWidget Pro orders get returned
                        if random.random() < 0.75:
                            ret_qty = qty  # Full return
                            ret_amount = round(ret_qty * price * (1 - discount), 2)
                            ret_day = min(day + random.randint(2, 7), 31)
                            returns_data.append(
                                (order_id_counter, line_id_counter, prod_id, ret_qty,
                                 ret_amount, f"2025-03-{ret_day:02d}", "defective_unit")
                            )
                    elif random.random() < 0.015:
                        # Normal low return rate
                        returns_data.append(
                            (order_id_counter, line_id_counter, prod_id, 1,
                             round(price * (1 - discount), 2),
                             f"2025-{month:02d}-{min(day + 3, days_in_month):02d}",
                             "customer_preference")
                        )

    c.executemany(
        "INSERT INTO returns (order_id, line_id, product_id, quantity_returned, return_amount, return_date, return_reason) VALUES (?,?,?,?,?,?,?)",
        returns_data,
    )

    conn.commit()

    # ---- Ground truth ----
    print("=== GROUND TRUTH ===")

    c.execute("SELECT SUM(line_total) FROM order_lines ol JOIN orders o ON ol.order_id=o.order_id WHERE o.order_date LIKE '2025-02%'")
    feb_gross = c.fetchone()[0]
    c.execute("SELECT SUM(line_total) FROM order_lines ol JOIN orders o ON ol.order_id=o.order_id WHERE o.order_date LIKE '2025-03%'")
    mar_gross = c.fetchone()[0]

    c.execute("SELECT SUM(return_amount) FROM returns WHERE return_date LIKE '2025-02%'")
    feb_ret = c.fetchone()[0] or 0
    c.execute("SELECT SUM(return_amount) FROM returns WHERE return_date LIKE '2025-03%'")
    mar_ret = c.fetchone()[0] or 0

    feb_net = feb_gross - feb_ret
    mar_net = mar_gross - mar_ret

    print(f"Feb gross: ${feb_gross:,.2f}  |  Mar gross: ${mar_gross:,.2f}  |  Gross delta: {((mar_gross-feb_gross)/feb_gross)*100:+.1f}%")
    print(f"Feb returns: ${feb_ret:,.2f}  |  Mar returns: ${mar_ret:,.2f}")
    print(f"Feb net: ${feb_net:,.2f}  |  Mar net: ${mar_net:,.2f}  |  Net delta: {((mar_net-feb_net)/feb_net)*100:+.1f}%")

    # Per-day comparison (normalizing for month length)
    feb_daily = feb_net / 28
    mar_daily = mar_net / 31
    print(f"Feb daily avg net: ${feb_daily:,.2f}  |  Mar daily avg net: ${mar_daily:,.2f}  |  Daily delta: {((mar_daily-feb_daily)/feb_daily)*100:+.1f}%")

    # Factor 1: Returns spike
    c.execute("SELECT SUM(return_amount) FROM returns WHERE return_date LIKE '2025-03%' AND product_id=7")
    p7_ret = c.fetchone()[0] or 0
    print(f"\nFactor 1 - UltraWidget Pro (id=7) returns in March: ${p7_ret:,.2f}")

    # Factor 2: Price cut on product 1
    c.execute("SELECT SUM(quantity) FROM order_lines ol JOIN orders o ON ol.order_id=o.order_id WHERE ol.product_id=1 AND o.order_date >= '2025-03-15'")
    p1_units_after = c.fetchone()[0] or 0
    price_impact = p1_units_after * (49.99 - 29.99)
    print(f"Factor 2 - CoreWidget price cut revenue impact: ${price_impact:,.2f} ({p1_units_after} units * $20 price reduction)")

    # Factor 3: SPRING_CLEAR discount
    c.execute("SELECT SUM(quantity * unit_price * discount_pct) FROM order_lines ol JOIN orders o ON ol.order_id=o.order_id WHERE o.order_date LIKE '2025-03%' AND promo_code='SPRING_CLEAR'")
    spring_disc = c.fetchone()[0] or 0
    c.execute("SELECT SUM(quantity * unit_price * discount_pct) FROM order_lines ol JOIN orders o ON ol.order_id=o.order_id WHERE o.order_date LIKE '2025-02%' AND promo_code IS NOT NULL")
    feb_disc = c.fetchone()[0] or 0
    print(f"Factor 3 - SPRING_CLEAR total discount: ${spring_disc:,.2f}  (Feb all promos: ${feb_disc:,.2f})")
    print(f"Incremental promo impact: ${spring_disc - feb_disc:,.2f}")

    conn.close()
    print(f"\nDB: {DB_PATH}")


if __name__ == "__main__":
    seed()
