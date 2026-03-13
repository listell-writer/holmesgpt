"""Seed a SQLite database with e-commerce data for product profitability analysis.

Ground truth: "Which product category is most profitable?"
The answer is NOT the highest-revenue category.

Schema complexity traps:
1. Revenue is in order_lines, but costs are spread across FOUR separate tables:
   - supplier_costs: per-unit cost from supplier (joined via product_id, but has date ranges -
     costs changed mid-quarter, must use the right cost for the right period)
   - shipping_costs: per-ORDER (not per-line), must be allocated proportionally across lines
   - warehouse_fees: monthly fees per product CATEGORY (not per product!) - joined via category name
   - returns: return amounts reduce net revenue (joined via line_id)

2. The join paths are intentionally non-obvious:
   - supplier_costs joins on product_id AND requires date-range matching
   - shipping_costs joins on order_id (shared across all lines in that order)
   - warehouse_fees joins on category name (string match to products.category)
   - returns join on line_id back to order_lines

3. The trick: Gadgets have highest revenue, but Sensors are most profitable because:
   - Gadgets have high supplier costs (65% COGS) + heavy shipping (bulky items)
   - Widgets have moderate margins but huge return rates (defective batch)
   - Sensors have lower revenue but tiny COGS (15%), negligible shipping, low returns

4. Extra confusion: there's a "cost_allocations" table that looks relevant but is
   actually internal accounting journal entries - using it would give wrong numbers.
"""

import sqlite3
import os
import random

DB_PATH = "/tmp/holmesgpt_eval_233.db"


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
        weight_kg REAL NOT NULL,
        created_at TEXT
    )""")

    products = [
        (1, "CoreWidget A", "Widgets", 0.5, "2024-01-15"),
        (2, "CoreWidget B", "Widgets", 0.8, "2024-01-15"),
        (3, "CoreWidget C", "Widgets", 0.3, "2024-02-01"),
        (4, "MegaGadget X", "Gadgets", 4.2, "2024-01-10"),
        (5, "MegaGadget Y", "Gadgets", 3.8, "2024-01-10"),
        (6, "MegaGadget Z", "Gadgets", 5.1, "2024-03-01"),
        (7, "NanoSensor P", "Sensors", 0.05, "2024-02-01"),
        (8, "NanoSensor Q", "Sensors", 0.08, "2024-02-01"),
        (9, "NanoSensor R", "Sensors", 0.03, "2024-04-01"),
        (10, "PowerPack Std", "Accessories", 1.2, "2024-05-01"),
        (11, "PowerPack Pro", "Accessories", 1.5, "2024-05-01"),
    ]
    c.executemany("INSERT INTO products VALUES (?,?,?,?,?)", products)

    # -- SUPPLIER_COSTS (per-unit cost with date ranges - costs change!) --
    c.execute("""
    CREATE TABLE supplier_costs (
        cost_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        unit_cost REAL NOT NULL,
        effective_date TEXT NOT NULL,
        end_date TEXT,
        supplier_name TEXT,
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""")

    # Gadgets: high COGS (~60-70% of price)
    # Widgets: moderate COGS (~40%)
    # Sensors: low COGS (~15%)
    # Accessories: moderate (~45%)
    supplier_costs = [
        # Widgets - moderate cost, one cost change
        (1, 12.00, "2024-01-01", "2025-02-28", "WidgetCorp"),
        (1, 14.00, "2025-03-01", None, "WidgetCorp"),
        (2, 18.00, "2024-01-01", None, "WidgetCorp"),
        (3, 8.00, "2024-01-01", None, "WidgetCorp"),
        # Gadgets - HIGH cost
        (4, 95.00, "2024-01-01", "2025-01-31", "GadgetSupply Inc"),
        (4, 105.00, "2025-02-01", None, "GadgetSupply Inc"),  # Cost went UP
        (5, 78.00, "2024-01-01", None, "GadgetSupply Inc"),
        (6, 120.00, "2024-01-01", None, "GadgetSupply Inc"),
        # Sensors - VERY LOW cost (high margin!)
        (7, 8.50, "2024-01-01", None, "NanoTech Ltd"),
        (8, 12.00, "2024-01-01", None, "NanoTech Ltd"),
        (9, 6.00, "2024-01-01", None, "NanoTech Ltd"),
        # Accessories - moderate
        (10, 15.00, "2024-01-01", None, "PackCo"),
        (11, 22.00, "2024-01-01", None, "PackCo"),
    ]
    c.executemany(
        "INSERT INTO supplier_costs (product_id, unit_cost, effective_date, end_date, supplier_name) VALUES (?,?,?,?,?)",
        supplier_costs,
    )

    # -- SHIPPING_COSTS (per ORDER, not per line item - must be allocated!) --
    c.execute("""
    CREATE TABLE shipping_costs (
        shipping_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        shipping_amount REAL NOT NULL,
        carrier TEXT,
        ship_date TEXT,
        FOREIGN KEY (order_id) REFERENCES orders(order_id)
    )""")

    # -- WAREHOUSE_FEES (monthly fee per CATEGORY - joins on category name) --
    c.execute("""
    CREATE TABLE warehouse_fees (
        fee_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        month TEXT NOT NULL,
        storage_fee REAL NOT NULL,
        handling_fee REAL NOT NULL
    )""")

    # Gadgets cost more to store (bulky), Sensors almost nothing
    for month in ["2025-01", "2025-02", "2025-03"]:
        c.execute("INSERT INTO warehouse_fees (category, month, storage_fee, handling_fee) VALUES (?,?,?,?)",
                  ("Widgets", month, 1200.00, 800.00))
        c.execute("INSERT INTO warehouse_fees (category, month, storage_fee, handling_fee) VALUES (?,?,?,?)",
                  ("Gadgets", month, 3500.00, 2200.00))
        c.execute("INSERT INTO warehouse_fees (category, month, storage_fee, handling_fee) VALUES (?,?,?,?)",
                  ("Sensors", month, 150.00, 100.00))
        c.execute("INSERT INTO warehouse_fees (category, month, storage_fee, handling_fee) VALUES (?,?,?,?)",
                  ("Accessories", month, 600.00, 400.00))

    # -- COST_ALLOCATIONS (red herring - internal accounting entries) --
    c.execute("""
    CREATE TABLE cost_allocations (
        allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        department TEXT NOT NULL,
        category TEXT NOT NULL,
        period TEXT NOT NULL,
        allocated_amount REAL NOT NULL,
        allocation_method TEXT,
        notes TEXT
    )""")

    # These are overhead allocations, NOT direct product costs
    for month in ["2025-01", "2025-02", "2025-03"]:
        for cat in ["Widgets", "Gadgets", "Sensors", "Accessories"]:
            c.execute(
                "INSERT INTO cost_allocations (department, category, period, allocated_amount, allocation_method, notes) VALUES (?,?,?,?,?,?)",
                ("Operations", cat, month, random.uniform(5000, 15000), "headcount_ratio",
                 "Monthly overhead allocation per corporate accounting policy"),
            )
            c.execute(
                "INSERT INTO cost_allocations (department, category, period, allocated_amount, allocation_method, notes) VALUES (?,?,?,?,?,?)",
                ("Marketing", cat, month, random.uniform(3000, 8000), "revenue_share",
                 "Marketing spend allocation based on revenue proportion"),
            )

    # -- ORDERS + ORDER_LINES --
    c.execute("""
    CREATE TABLE orders (
        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        order_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed'
    )""")

    c.execute("""
    CREATE TABLE order_lines (
        line_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        line_total REAL NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(order_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""")

    # -- RETURNS --
    c.execute("""
    CREATE TABLE returns (
        return_id INTEGER PRIMARY KEY AUTOINCREMENT,
        line_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity_returned INTEGER NOT NULL,
        return_amount REAL NOT NULL,
        return_date TEXT NOT NULL,
        return_reason TEXT,
        FOREIGN KEY (line_id) REFERENCES order_lines(line_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""")

    random.seed(123)

    # Selling prices
    sell_price = {
        1: 29.99, 2: 44.99, 3: 19.99,      # Widgets - low-mid price
        4: 149.99, 5: 119.99, 6: 189.99,    # Gadgets - high price
        7: 54.99, 8: 79.99, 9: 39.99,       # Sensors - mid price
        10: 34.99, 11: 49.99,               # Accessories - low-mid
    }

    # Product selection weights (Gadgets sell more units to create highest revenue)
    weights = [1, 1, 1, 4, 4, 4, 4, 4, 7, 7, 7, 8, 8, 8, 9, 9, 10, 11]

    order_id = 0
    line_id = 0
    returns_data = []
    shipping_data = []

    for month in [1, 2, 3]:
        days_in_month = {1: 31, 2: 28, 3: 31}[month]

        for day in range(1, days_in_month + 1):
            date_str = f"2025-{month:02d}-{day:02d}"
            n_orders = random.randint(10, 14)

            for _ in range(n_orders):
                order_id += 1
                cust_id = random.randint(1, 100)
                c.execute("INSERT INTO orders VALUES (?,?,?,?)",
                          (order_id, cust_id, date_str, "completed"))

                n_lines = random.randint(1, 3)
                order_total = 0
                order_has_gadget = False

                for _ in range(n_lines):
                    line_id += 1
                    prod_id = random.choice(weights)
                    qty = random.randint(1, 4)
                    price = sell_price[prod_id]
                    total = round(qty * price, 2)
                    order_total += total

                    if prod_id in [4, 5, 6]:
                        order_has_gadget = True

                    c.execute("INSERT INTO order_lines VALUES (?,?,?,?,?,?)",
                              (line_id, order_id, prod_id, qty, price, total))

                    # Returns: Widgets have HIGH return rate (10%), others low
                    cat = products[[p[0] for p in products].index(prod_id)][2]
                    if cat == "Widgets" and random.random() < 0.10:
                        ret_qty = min(qty, random.randint(1, 2))
                        ret_amt = round(ret_qty * price, 2)
                        ret_day = min(day + random.randint(3, 10), days_in_month)
                        returns_data.append(
                            (line_id, prod_id, ret_qty, ret_amt,
                             f"2025-{month:02d}-{ret_day:02d}", "quality_issue")
                        )
                    elif random.random() < 0.01:
                        returns_data.append(
                            (line_id, prod_id, 1, round(price, 2),
                             f"2025-{month:02d}-{min(day + 5, days_in_month):02d}",
                             "changed_mind")
                        )

                # Shipping: Gadgets are bulky = expensive shipping
                if order_has_gadget:
                    ship_amt = round(random.uniform(12.00, 25.00), 2)
                else:
                    ship_amt = round(random.uniform(3.50, 7.50), 2)

                shipping_data.append(
                    (order_id, ship_amt, random.choice(["FastShip", "EcoFreight", "ExpressGo"]),
                     date_str)
                )

    c.executemany(
        "INSERT INTO returns (line_id, product_id, quantity_returned, return_amount, return_date, return_reason) VALUES (?,?,?,?,?,?)",
        returns_data,
    )
    c.executemany(
        "INSERT INTO shipping_costs (order_id, shipping_amount, carrier, ship_date) VALUES (?,?,?,?)",
        shipping_data,
    )

    conn.commit()

    # ---- Compute ground truth profitability by category ----
    print("=== GROUND TRUTH: PROFITABILITY BY CATEGORY (Q1 2025) ===\n")

    categories = ["Widgets", "Gadgets", "Sensors", "Accessories"]

    for cat in categories:
        # Revenue
        c.execute("""
            SELECT COALESCE(SUM(ol.line_total), 0)
            FROM order_lines ol
            JOIN products p ON ol.product_id = p.product_id
            WHERE p.category = ?
        """, (cat,))
        revenue = c.fetchone()[0]

        # Returns
        c.execute("""
            SELECT COALESCE(SUM(r.return_amount), 0)
            FROM returns r
            JOIN products p ON r.product_id = p.product_id
            WHERE p.category = ?
        """, (cat,))
        returns_amt = c.fetchone()[0]

        net_revenue = revenue - returns_amt

        # COGS (supplier costs with date matching)
        # Simplified: use quantity * cost for the applicable period
        c.execute("""
            SELECT COALESCE(SUM(
                ol.quantity * (
                    SELECT sc.unit_cost FROM supplier_costs sc
                    WHERE sc.product_id = ol.product_id
                    AND sc.effective_date <= o.order_date
                    AND (sc.end_date IS NULL OR sc.end_date >= o.order_date)
                    LIMIT 1
                )
            ), 0)
            FROM order_lines ol
            JOIN orders o ON ol.order_id = o.order_id
            JOIN products p ON ol.product_id = p.product_id
            WHERE p.category = ?
        """, (cat,))
        cogs = c.fetchone()[0]

        # Shipping (allocated proportionally by line_total within each order)
        c.execute("""
            SELECT COALESCE(SUM(
                sc.shipping_amount * (ol.line_total / order_totals.order_total)
            ), 0)
            FROM order_lines ol
            JOIN products p ON ol.product_id = p.product_id
            JOIN shipping_costs sc ON ol.order_id = sc.order_id
            JOIN (
                SELECT order_id, SUM(line_total) as order_total
                FROM order_lines GROUP BY order_id
            ) order_totals ON ol.order_id = order_totals.order_id
            WHERE p.category = ? AND order_totals.order_total > 0
        """, (cat,))
        shipping = c.fetchone()[0]

        # Warehouse fees
        c.execute("""
            SELECT COALESCE(SUM(storage_fee + handling_fee), 0)
            FROM warehouse_fees WHERE category = ?
        """, (cat,))
        warehouse = c.fetchone()[0]

        gross_profit = net_revenue - cogs - shipping - warehouse
        margin = (gross_profit / net_revenue * 100) if net_revenue > 0 else 0

        print(f"{cat}:")
        print(f"  Revenue:      ${revenue:>12,.2f}")
        print(f"  Returns:      ${returns_amt:>12,.2f}")
        print(f"  Net Revenue:  ${net_revenue:>12,.2f}")
        print(f"  COGS:         ${cogs:>12,.2f}")
        print(f"  Shipping:     ${shipping:>12,.2f}")
        print(f"  Warehouse:    ${warehouse:>12,.2f}")
        print(f"  Gross Profit: ${gross_profit:>12,.2f}  ({margin:.1f}% margin)")
        print()

    conn.close()
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    seed()
