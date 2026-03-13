"""Seed a SQLite database with e-commerce data for product profitability analysis.

Ground truth: "Which product category is most profitable?"
The answer is NOT the highest-revenue category, NOR the highest-margin-looking one.

Schema complexity traps:

1. Revenue is in order_lines (straightforward), but costs require multi-hop joins:
   - procurement_orders → procurement_line_items → products (COGS, joined through PO system)
   - fulfillment_batches → shipment_charges (shipping costs linked to BATCHES not orders)
   - facility_rates (monthly per facility) → products.warehouse_id (join through facility assignment)
   - order_adjustments (returns/credits, type='return', linked by order_line_id)

2. Red herring tables:
   - financial_summary: pre-computed "profit" numbers that are WRONG (uses outdated cost basis)
   - budget_forecast: planning numbers, not actuals
   - cost_allocations: corporate overhead, not product costs

3. The twist that makes naive analysis fail:
   - Gadgets: Highest revenue ($225K), looks like ~45% margin if you use financial_summary (WRONG)
   - Sensors: Medium revenue ($144K), actual highest profit ($117K, 81.8% margin)
   - Electronics: NEW category - medium revenue ($95K), looks profitable at first glance
     but has hidden costs in fulfillment_batches (special handling surcharges) and
     high return rate in order_adjustments

4. procurement_line_items has unit_cost that changed over time (via different POs),
   and the LLM must match PO dates to order dates to get accurate COGS.
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

    # -- PRODUCTS (note: warehouse_id is the FK to facility_rates) --
    c.execute("""
    CREATE TABLE products (
        product_id INTEGER PRIMARY KEY,
        sku TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        warehouse_id INTEGER NOT NULL,
        weight_kg REAL NOT NULL,
        is_active INTEGER DEFAULT 1
    )""")

    products = [
        (1, "WDG-001", "CoreWidget A", "Widgets", 1, 0.5),
        (2, "WDG-002", "CoreWidget B", "Widgets", 1, 0.8),
        (3, "WDG-003", "CoreWidget C", "Widgets", 1, 0.3),
        (4, "GDG-001", "MegaGadget X", "Gadgets", 2, 4.2),
        (5, "GDG-002", "MegaGadget Y", "Gadgets", 2, 3.8),
        (6, "GDG-003", "MegaGadget Z", "Gadgets", 2, 5.1),
        (7, "SNS-001", "NanoSensor P", "Sensors", 3, 0.05),
        (8, "SNS-002", "NanoSensor Q", "Sensors", 3, 0.08),
        (9, "SNS-003", "NanoSensor R", "Sensors", 3, 0.03),
        (10, "ELC-001", "SmartBoard V1", "Electronics", 4, 1.8),
        (11, "ELC-002", "SmartBoard V2", "Electronics", 4, 2.1),
        (12, "ELC-003", "SmartDisplay Pro", "Electronics", 4, 3.2),
        (13, "ACC-001", "PowerPack Std", "Accessories", 1, 1.2),
        (14, "ACC-002", "PowerPack Pro", "Accessories", 1, 1.5),
    ]
    c.executemany("INSERT INTO products (product_id, sku, name, category, warehouse_id, weight_kg) VALUES (?,?,?,?,?,?)", products)

    # -- PROCUREMENT_ORDERS (purchase orders to suppliers - NOT customer orders) --
    c.execute("""
    CREATE TABLE procurement_orders (
        po_id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_name TEXT NOT NULL,
        order_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'received'
    )""")

    # -- PROCUREMENT_LINE_ITEMS (what was ordered at what cost) --
    c.execute("""
    CREATE TABLE procurement_line_items (
        po_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_cost REAL NOT NULL,
        FOREIGN KEY (po_id) REFERENCES procurement_orders(po_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""")

    # Create procurement orders with different costs at different times
    # Gadgets: high cost (~65% of price)
    # Sensors: very low cost (~15% of price)
    # Electronics: moderate cost (~40%) BUT with hidden fulfillment surcharges
    # Widgets: moderate cost (~40%)

    po_data = [
        # Q4 2024 procurement (lower costs)
        ("WidgetCorp", "2024-10-15", [
            (1, 500, 12.00), (2, 300, 18.00), (3, 400, 8.00),
        ]),
        ("GadgetSupply Inc", "2024-10-20", [
            (4, 200, 90.00), (5, 250, 75.00), (6, 150, 115.00),
        ]),
        ("NanoTech Ltd", "2024-11-01", [
            (7, 600, 8.50), (8, 500, 12.00), (9, 400, 6.00),
        ]),
        ("TechComponents AG", "2024-11-15", [
            (10, 300, 38.00), (11, 250, 42.00), (12, 200, 55.00),
        ]),
        ("PackCo", "2024-12-01", [
            (13, 400, 15.00), (14, 300, 22.00),
        ]),
        # Q1 2025 procurement (Gadget costs went UP, others stable)
        ("WidgetCorp", "2025-01-20", [
            (1, 500, 14.00), (2, 300, 18.00), (3, 400, 8.00),
        ]),
        ("GadgetSupply Inc", "2025-02-05", [
            (4, 200, 105.00), (5, 250, 78.00), (6, 150, 120.00),
        ]),
        ("NanoTech Ltd", "2025-02-10", [
            (7, 600, 8.50), (8, 500, 12.00), (9, 400, 6.00),
        ]),
        ("TechComponents AG", "2025-02-15", [
            (10, 300, 38.00), (11, 250, 42.00), (12, 200, 55.00),
        ]),
    ]

    for supplier, date, lines in po_data:
        c.execute("INSERT INTO procurement_orders (supplier_name, order_date) VALUES (?,?)",
                  (supplier, date))
        po_id = c.lastrowid
        for prod_id, qty, cost in lines:
            c.execute("INSERT INTO procurement_line_items (po_id, product_id, quantity, unit_cost) VALUES (?,?,?,?)",
                      (po_id, prod_id, qty, cost))

    # -- FULFILLMENT_BATCHES (shipping through batches, NOT per-order) --
    c.execute("""
    CREATE TABLE fulfillment_batches (
        batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_date TEXT NOT NULL,
        carrier TEXT NOT NULL,
        batch_type TEXT NOT NULL DEFAULT 'standard'
    )""")

    # -- SHIPMENT_CHARGES (charges per batch, must be allocated to orders in that batch) --
    c.execute("""
    CREATE TABLE shipment_charges (
        charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL,
        charge_type TEXT NOT NULL,
        amount REAL NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES fulfillment_batches(batch_id)
    )""")

    # -- BATCH_ORDERS (which orders are in which batch - the bridge table) --
    c.execute("""
    CREATE TABLE batch_orders (
        batch_id INTEGER NOT NULL,
        order_id INTEGER NOT NULL,
        PRIMARY KEY (batch_id, order_id),
        FOREIGN KEY (batch_id) REFERENCES fulfillment_batches(batch_id)
    )""")

    # -- FACILITY_RATES (warehousing costs per facility per month) --
    c.execute("""
    CREATE TABLE facility_rates (
        rate_id INTEGER PRIMARY KEY AUTOINCREMENT,
        warehouse_id INTEGER NOT NULL,
        month TEXT NOT NULL,
        storage_cost REAL NOT NULL,
        handling_cost REAL NOT NULL,
        special_handling_surcharge REAL NOT NULL DEFAULT 0
    )""")

    # Warehouse 1 (Widgets+Accessories): moderate
    # Warehouse 2 (Gadgets): expensive (bulky)
    # Warehouse 3 (Sensors): cheap (tiny)
    # Warehouse 4 (Electronics): moderate base BUT has special_handling_surcharge
    for month in ["2025-01", "2025-02", "2025-03"]:
        c.execute("INSERT INTO facility_rates (warehouse_id, month, storage_cost, handling_cost, special_handling_surcharge) VALUES (?,?,?,?,?)",
                  (1, month, 1200.00, 800.00, 0))
        c.execute("INSERT INTO facility_rates (warehouse_id, month, storage_cost, handling_cost, special_handling_surcharge) VALUES (?,?,?,?,?)",
                  (2, month, 3500.00, 2200.00, 0))
        c.execute("INSERT INTO facility_rates (warehouse_id, month, storage_cost, handling_cost, special_handling_surcharge) VALUES (?,?,?,?,?)",
                  (3, month, 150.00, 100.00, 0))
        c.execute("INSERT INTO facility_rates (warehouse_id, month, storage_cost, handling_cost, special_handling_surcharge) VALUES (?,?,?,?,?)",
                  (4, month, 1800.00, 1200.00, 2500.00))  # Electronics: special handling!

    # -- ORDER_ADJUSTMENTS (returns, credits, chargebacks - all in one table) --
    c.execute("""
    CREATE TABLE order_adjustments (
        adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_line_id INTEGER NOT NULL,
        adjustment_type TEXT NOT NULL,
        amount REAL NOT NULL,
        adjustment_date TEXT NOT NULL,
        reason TEXT,
        FOREIGN KEY (order_line_id) REFERENCES order_lines(line_id)
    )""")

    # -- RED HERRING: financial_summary (pre-computed but WRONG numbers) --
    c.execute("""
    CREATE TABLE financial_summary (
        summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        period TEXT NOT NULL,
        total_revenue REAL NOT NULL,
        total_cost REAL NOT NULL,
        gross_profit REAL NOT NULL,
        profit_margin REAL NOT NULL,
        notes TEXT
    )""")

    # Deliberately WRONG profit numbers (uses outdated cost basis from 2024)
    wrong_summaries = [
        ("Widgets", "Q1-2025", 32000, 11000, 21000, 0.656, "Based on 2024 standard costs"),
        ("Gadgets", "Q1-2025", 225000, 125000, 100000, 0.444, "Based on 2024 standard costs"),
        ("Sensors", "Q1-2025", 144000, 50000, 94000, 0.653, "Based on 2024 standard costs"),
        ("Electronics", "Q1-2025", 95000, 40000, 55000, 0.579, "Based on 2024 standard costs"),
        ("Accessories", "Q1-2025", 24000, 11000, 13000, 0.542, "Based on 2024 standard costs"),
    ]
    c.executemany("INSERT INTO financial_summary (category, period, total_revenue, total_cost, gross_profit, profit_margin, notes) VALUES (?,?,?,?,?,?,?)",
                  wrong_summaries)

    # -- RED HERRING: budget_forecast --
    c.execute("""
    CREATE TABLE budget_forecast (
        forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        quarter TEXT NOT NULL,
        projected_revenue REAL,
        projected_cost REAL,
        projected_margin REAL
    )""")

    for cat in ["Widgets", "Gadgets", "Sensors", "Electronics", "Accessories"]:
        c.execute("INSERT INTO budget_forecast (category, quarter, projected_revenue, projected_cost, projected_margin) VALUES (?,?,?,?,?)",
                  (cat, "Q1-2025", random.uniform(20000, 250000), random.uniform(10000, 150000), random.uniform(0.2, 0.8)))

    # -- RED HERRING: cost_allocations --
    c.execute("""
    CREATE TABLE cost_allocations (
        allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        department TEXT NOT NULL,
        category TEXT NOT NULL,
        period TEXT NOT NULL,
        allocated_amount REAL NOT NULL,
        allocation_method TEXT
    )""")

    random.seed(456)
    for month in ["2025-01", "2025-02", "2025-03"]:
        for cat in ["Widgets", "Gadgets", "Sensors", "Electronics", "Accessories"]:
            c.execute("INSERT INTO cost_allocations (department, category, period, allocated_amount, allocation_method) VALUES (?,?,?,?,?)",
                      ("Operations", cat, month, random.uniform(5000, 15000), "headcount_ratio"))

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

    random.seed(123)

    sell_price = {
        1: 29.99, 2: 44.99, 3: 19.99,       # Widgets
        4: 149.99, 5: 119.99, 6: 189.99,     # Gadgets
        7: 54.99, 8: 79.99, 9: 39.99,        # Sensors
        10: 89.99, 11: 109.99, 12: 139.99,   # Electronics
        13: 34.99, 14: 49.99,                 # Accessories
    }

    # Weight to make Gadgets highest revenue, Sensors second, Electronics third
    weights = [1, 1, 1, 4, 4, 4, 4, 4, 7, 7, 7, 8, 8, 8, 9, 9, 10, 10, 11, 11, 12, 13, 14]

    order_id = 0
    line_id = 0
    adjustments_data = []
    batch_id = 0
    batch_orders_data = []
    charges_data = []

    for month in [1, 2, 3]:
        days_in_month = {1: 31, 2: 28, 3: 31}[month]

        for day in range(1, days_in_month + 1):
            date_str = f"2025-{month:02d}-{day:02d}"
            n_orders = random.randint(10, 14)

            # Create a fulfillment batch every day
            batch_id += 1
            has_electronics = False
            batch_has_gadget = False
            c.execute("INSERT INTO fulfillment_batches (batch_date, carrier, batch_type) VALUES (?,?,?)",
                      (date_str, random.choice(["FastShip", "EcoFreight", "ExpressGo"]), "standard"))

            daily_order_ids = []

            for _ in range(n_orders):
                order_id += 1
                cust_id = random.randint(1, 100)
                c.execute("INSERT INTO orders VALUES (?,?,?,?)",
                          (order_id, cust_id, date_str, "completed"))
                daily_order_ids.append(order_id)
                batch_orders_data.append((batch_id, order_id))

                n_lines = random.randint(1, 3)

                for _ in range(n_lines):
                    line_id += 1
                    prod_id = random.choice(weights)
                    qty = random.randint(1, 4)
                    price = sell_price[prod_id]
                    total = round(qty * price, 2)

                    cat = [p[4] for p in products if p[0] == prod_id][0]
                    if cat == "Gadgets":
                        batch_has_gadget = True
                    if cat == "Electronics":
                        has_electronics = True

                    c.execute("INSERT INTO order_lines VALUES (?,?,?,?,?,?)",
                              (line_id, order_id, prod_id, qty, price, total))

                    # Returns: Electronics have HIGH return rate (15%)
                    # Widgets moderate (10%), others low
                    if cat == "Electronics" and random.random() < 0.15:
                        ret_qty = min(qty, random.randint(1, 2))
                        ret_amt = round(ret_qty * price, 2)
                        ret_day = min(day + random.randint(3, 10), days_in_month)
                        adjustments_data.append(
                            (line_id, "return", ret_amt,
                             f"2025-{month:02d}-{ret_day:02d}", "defective_unit"))
                    elif cat == "Widgets" and random.random() < 0.10:
                        ret_qty = min(qty, random.randint(1, 2))
                        ret_amt = round(ret_qty * price, 2)
                        ret_day = min(day + random.randint(3, 10), days_in_month)
                        adjustments_data.append(
                            (line_id, "return", ret_amt,
                             f"2025-{month:02d}-{ret_day:02d}", "quality_issue"))
                    elif random.random() < 0.01:
                        adjustments_data.append(
                            (line_id, "return", round(price, 2),
                             f"2025-{month:02d}-{min(day + 5, days_in_month):02d}",
                             "changed_mind"))

                    # Occasional credit adjustments (not returns)
                    if random.random() < 0.02:
                        adjustments_data.append(
                            (line_id, "credit", round(price * 0.1, 2),
                             f"2025-{month:02d}-{min(day + 2, days_in_month):02d}",
                             "goodwill_credit"))

            # Shipment charges for this batch
            # Base freight
            if batch_has_gadget:
                base_freight = round(random.uniform(80.00, 150.00), 2)
            else:
                base_freight = round(random.uniform(25.00, 55.00), 2)
            charges_data.append((batch_id, "freight", base_freight))

            # Handling surcharge for electronics (fragile)
            if has_electronics:
                surcharge = round(random.uniform(15.00, 35.00), 2)
                charges_data.append((batch_id, "special_handling", surcharge))

            # Fuel surcharge (universal)
            charges_data.append((batch_id, "fuel_surcharge", round(base_freight * 0.08, 2)))

    c.executemany("INSERT INTO order_adjustments (order_line_id, adjustment_type, amount, adjustment_date, reason) VALUES (?,?,?,?,?)",
                  adjustments_data)
    c.executemany("INSERT INTO batch_orders VALUES (?,?)", batch_orders_data)
    c.executemany("INSERT INTO shipment_charges (batch_id, charge_type, amount) VALUES (?,?,?)",
                  charges_data)

    conn.commit()

    # ---- Compute ground truth profitability by category ----
    print("=== GROUND TRUTH: PROFITABILITY BY CATEGORY (Q1 2025) ===\n")

    categories = ["Widgets", "Gadgets", "Sensors", "Electronics", "Accessories"]

    for cat in categories:
        # Revenue
        c.execute("""
            SELECT COALESCE(SUM(ol.line_total), 0)
            FROM order_lines ol
            JOIN products p ON ol.product_id = p.product_id
            WHERE p.category = ?
        """, (cat,))
        revenue = c.fetchone()[0]

        # Returns + credits (from order_adjustments)
        c.execute("""
            SELECT COALESCE(SUM(oa.amount), 0)
            FROM order_adjustments oa
            JOIN order_lines ol ON oa.order_line_id = ol.line_id
            JOIN products p ON ol.product_id = p.product_id
            WHERE p.category = ? AND oa.adjustment_type = 'return'
        """, (cat,))
        returns_amt = c.fetchone()[0]

        net_revenue = revenue - returns_amt

        # COGS (from procurement - use latest PO cost per product)
        c.execute("""
            SELECT COALESCE(SUM(
                ol.quantity * (
                    SELECT pli.unit_cost
                    FROM procurement_line_items pli
                    JOIN procurement_orders po ON pli.po_id = po.po_id
                    WHERE pli.product_id = ol.product_id
                    AND po.order_date <= o.order_date
                    ORDER BY po.order_date DESC
                    LIMIT 1
                )
            ), 0)
            FROM order_lines ol
            JOIN orders o ON ol.order_id = o.order_id
            JOIN products p ON ol.product_id = p.product_id
            WHERE p.category = ?
        """, (cat,))
        cogs = c.fetchone()[0]

        # Shipping (allocated from fulfillment batches proportionally)
        c.execute("""
            SELECT COALESCE(SUM(
                total_charge * (cat_revenue / batch_revenue)
            ), 0) FROM (
                SELECT
                    bo.batch_id,
                    (SELECT SUM(sc.amount) FROM shipment_charges sc WHERE sc.batch_id = bo.batch_id) as total_charge,
                    SUM(CASE WHEN p.category = ? THEN ol.line_total ELSE 0 END) as cat_revenue,
                    SUM(ol.line_total) as batch_revenue
                FROM batch_orders bo
                JOIN order_lines ol ON bo.order_id = ol.order_id
                JOIN products p ON ol.product_id = p.product_id
                GROUP BY bo.batch_id
                HAVING batch_revenue > 0
            )
        """, (cat,))
        shipping = c.fetchone()[0]

        # Warehouse fees (from facility_rates via products.warehouse_id)
        c.execute("""
            SELECT COALESCE(SUM(fr.storage_cost + fr.handling_cost + fr.special_handling_surcharge), 0)
            FROM facility_rates fr
            WHERE fr.warehouse_id IN (
                SELECT DISTINCT p.warehouse_id FROM products p WHERE p.category = ?
            )
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

    # Show the WRONG financial_summary for comparison
    print("=== RED HERRING: financial_summary (WRONG NUMBERS) ===")
    c.execute("SELECT category, gross_profit, profit_margin, notes FROM financial_summary")
    for row in c.fetchall():
        print(f"  {row[0]}: profit=${row[1]:,.0f}, margin={row[2]:.1%} ({row[3]})")

    conn.close()
    print(f"\nDB: {DB_PATH}")


if __name__ == "__main__":
    seed()
