"""
AGRODIV — Plateforme de gestion des flux commerciaux intra et inter CIC
Filiale Céréales Ouest (FCO)

Single-file Streamlit + SQLite application.

Installation:
    pip install streamlit pandas

Run:
    streamlit run app.py

Default demo accounts:
    admin / admin123
    manager / manager123
    agent / agent123
"""

import csv
import hashlib
import io
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from typing import Optional, Dict, Any, List

import pandas as pd
import streamlit as st


# =============================================================================
# CONFIGURATION
# =============================================================================

APP_TITLE = "Plateforme de gestion des flux commerciaux intra et inter CIC"
COMPANY_NAME = "AGRODIV — Filiale Céréales Ouest"
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agrodiv_flux.db")

STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_IN_TRANSIT = "In Transit"
STATUS_RECEIVED = "Received"
STATUS_COMPLETED = "Completed"
STATUS_REJECTED = "Rejected"
STATUS_CANCELLED = "Cancelled"

ALL_STATUSES = [
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_IN_TRANSIT,
    STATUS_RECEIVED,
    STATUS_COMPLETED,
    STATUS_REJECTED,
    STATUS_CANCELLED,
]

ROLES = ["Admin", "Manager CIC", "Agent Commercial"]

AGRODIV_GREEN = "#15803D"
AGRODIV_DARK_GREEN = "#166534"
AGRODIV_NAVY = "#0F172A"
AGRODIV_LIGHT = "#F0FDF4"
AGRODIV_BORDER = "#D1D5DB"
AGRODIV_RED = "#DC2626"
AGRODIV_ORANGE = "#EA580C"
AGRODIV_BLUE = "#2563EB"


# =============================================================================
# DATABASE
# =============================================================================

@contextmanager
def get_db():
    """Open a SQLite connection and automatically close it."""
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Create a salted PBKDF2 password hash."""
    if salt is None:
        salt = secrets.token_hex(16)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()

    return f"{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored PBKDF2 hash."""
    try:
        salt, expected = stored_hash.split("$", 1)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            120_000,
        ).hex()

        return secrets.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


def init_database():
    """Create all application tables and insert initial demo data."""
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN (
                    'Admin',
                    'Manager CIC',
                    'Agent Commercial'
                )),
                cic_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(cic_id) REFERENCES cics(id)
            );

            CREATE TABLE IF NOT EXISTS cics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                complex_name TEXT NOT NULL,
                wilaya TEXT,
                address TEXT,
                manager_name TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'Tonnes',
                minimum_stock REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cic_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cic_id, product_id),
                FOREIGN KEY(cic_id) REFERENCES cics(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT UNIQUE NOT NULL,
                transfer_type TEXT NOT NULL CHECK(
                    transfer_type IN ('Intra-CIC', 'Inter-CIC')
                ),
                source_cic_id INTEGER NOT NULL,
                destination_cic_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity REAL NOT NULL CHECK(quantity > 0),
                transfer_date TEXT NOT NULL,
                expected_date TEXT,
                status TEXT NOT NULL DEFAULT 'Pending',
                reason TEXT,
                created_by INTEGER NOT NULL,
                approved_by INTEGER,
                approved_at TEXT,
                received_by INTEGER,
                received_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(source_cic_id) REFERENCES cics(id),
                FOREIGN KEY(destination_cic_id) REFERENCES cics(id),
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(approved_by) REFERENCES users(id),
                FOREIGN KEY(received_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                action TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                user_id INTEGER,
                username TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_transfers_status
                ON transfers(status);

            CREATE INDEX IF NOT EXISTS idx_transfers_source
                ON transfers(source_cic_id);

            CREATE INDEX IF NOT EXISTS idx_transfers_destination
                ON transfers(destination_cic_id);

            CREATE INDEX IF NOT EXISTS idx_audit_entity
                ON audit_logs(entity_type, entity_id);

            CREATE INDEX IF NOT EXISTS idx_stocks_cic_product
                ON stocks(cic_id, product_id);
            """
        )

        # ---------------------------------------------------------------------
        # CIC SAMPLE DATA
        # ---------------------------------------------------------------------
        cics = [
            (
                "CIC-SBA",
                "CIC Sidi Bel Abbès",
                "Complexe Sidi Bel Abbès",
                "Sidi Bel Abbès",
                "63 Avenue Aissat Idir",
                "Direction CIC SBA",
            ),
            (
                "CIC-ORN",
                "CIC Oran",
                "Complexe Oran",
                "Oran",
                "Zone Industrielle Oran",
                "Direction CIC Oran",
            ),
            (
                "CIC-BEC",
                "CIC Béchar",
                "Complexe Béchar",
                "Béchar",
                "Zone Industrielle Béchar",
                "Direction CIC Béchar",
            ),
            (
                "CIC-TLM",
                "CIC Tlemcen",
                "Complexe Tlemcen",
                "Tlemcen",
                "Zone Industrielle Tlemcen",
                "Direction CIC Tlemcen",
            ),
            (
                "CIC-MSC",
                "CIC Mascara",
                "Complexe Mascara",
                "Mascara",
                "Zone Industrielle Mascara",
                "Direction CIC Mascara",
            ),
        ]

        for row in cics:
            conn.execute(
                """
                INSERT OR IGNORE INTO cics
                (code, name, complex_name, wilaya, address, manager_name)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                row,
            )

        # ---------------------------------------------------------------------
        # PRODUCT SAMPLE DATA
        # ---------------------------------------------------------------------
        products = [
            ("BT-001", "Blé Tendre", "Céréales", "Tonnes", 100),
            ("BD-001", "Blé Dur", "Céréales", "Tonnes", 100),
            ("ORG-001", "Orge", "Céréales", "Tonnes", 75),
            ("MAI-001", "Maïs", "Céréales", "Tonnes", 50),
            ("FAR-001", "Farine", "Produits finis", "Tonnes", 50),
            ("SEM-001", "Semoule", "Produits finis", "Tonnes", 50),
            ("SON-001", "Son de blé", "Sous-produits", "Tonnes", 25),
        ]

        for row in products:
            conn.execute(
                """
                INSERT OR IGNORE INTO products
                (code, name, category, unit, minimum_stock)
                VALUES (?, ?, ?, ?, ?)
                """,
                row,
            )

        # ---------------------------------------------------------------------
        # STOCK SAMPLE DATA
        # ---------------------------------------------------------------------
        cic_rows = conn.execute(
            "SELECT id, code FROM cics ORDER BY id"
        ).fetchall()

        product_rows = conn.execute(
            "SELECT id, code FROM products ORDER BY id"
        ).fetchall()

        stock_values = {
            "CIC-SBA": {
                "BT-001": 1850,
                "BD-001": 920,
                "ORG-001": 540,
                "MAI-001": 280,
                "FAR-001": 620,
                "SEM-001": 450,
                "SON-001": 130,
            },
            "CIC-ORN": {
                "BT-001": 1250,
                "BD-001": 670,
                "ORG-001": 410,
                "MAI-001": 350,
                "FAR-001": 420,
                "SEM-001": 330,
                "SON-001": 90,
            },
            "CIC-BEC": {
                "BT-001": 780,
                "BD-001": 310,
                "ORG-001": 260,
                "MAI-001": 180,
                "FAR-001": 210,
                "SEM-001": 170,
                "SON-001": 55,
            },
            "CIC-TLM": {
                "BT-001": 920,
                "BD-001": 480,
                "ORG-001": 300,
                "MAI-001": 240,
                "FAR-001": 275,
                "SEM-001": 230,
                "SON-001": 75,
            },
            "CIC-MSC": {
                "BT-001": 690,
                "BD-001": 390,
                "ORG-001": 220,
                "MAI-001": 160,
                "FAR-001": 190,
                "SEM-001": 145,
                "SON-001": 50,
            },
        }

        for cic in cic_rows:
            for product in product_rows:
                quantity = stock_values.get(cic["code"], {}).get(
                    product["code"], 0
                )

                conn.execute(
                    """
                    INSERT OR IGNORE INTO stocks
                    (cic_id, product_id, quantity)
                    VALUES (?, ?, ?)
                    """,
                    (cic["id"], product["id"], quantity),
                )

        # ---------------------------------------------------------------------
        # DEMO USERS
        # ---------------------------------------------------------------------
        sba_id = conn.execute(
            "SELECT id FROM cics WHERE code = 'CIC-SBA'"
        ).fetchone()["id"]

        orn_id = conn.execute(
            "SELECT id FROM cics WHERE code = 'CIC-ORN'"
        ).fetchone()["id"]

        users = [
            (
                "admin",
                "Administrateur AGRODIV",
                hash_password("admin123"),
                "Admin",
                None,
            ),
            (
                "manager",
                "Manager CIC Sidi Bel Abbès",
                hash_password("manager123"),
                "Manager CIC",
                sba_id,
            ),
            (
                "manager_oran",
                "Manager CIC Oran",
                hash_password("manager123"),
                "Manager CIC",
                orn_id,
            ),
            (
                "agent",
                "Agent Commercial SBA",
                hash_password("agent123"),
                "Agent Commercial",
                sba_id,
            ),
        ]

        for row in users:
            conn.execute(
                """
                INSERT OR IGNORE INTO users
                (username, full_name, password_hash, role, cic_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                row,
            )


# =============================================================================
# AUDIT
# =============================================================================

def write_audit(
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
):
    """Write an immutable audit record."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs
            (
                entity_type,
                entity_id,
                action,
                old_value,
                new_value,
                user_id,
                username
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                action,
                old_value,
                new_value,
                user_id,
                username,
            ),
        )


# =============================================================================
# AUTHENTICATION
# =============================================================================

def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticate a user and return the user record."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
              AND active = 1
            """,
            (username.strip(),),
        ).fetchone()

    if row and verify_password(password, row["password_hash"]):
        return dict(row)

    return None


def can_manage_transfers(user: Dict[str, Any]) -> bool:
    """Return whether the user can validate transfer workflow actions."""
    return user["role"] in ("Admin", "Manager CIC")


def can_manage_products(user: Dict[str, Any]) -> bool:
    """Return whether the user can create or edit products."""
    return user["role"] in ("Admin", "Manager CIC")


def user_can_access_transfer(user: Dict[str, Any], transfer: Dict[str, Any]) -> bool:
    """Check whether a user can access a transfer."""
    if user["role"] == "Admin":
        return True

    cic_id = user.get("cic_id")

    return cic_id in (
        transfer.get("source_cic_id"),
        transfer.get("destination_cic_id"),
    )


# =============================================================================
# DATA ACCESS
# =============================================================================

def get_cics(active_only: bool = True) -> pd.DataFrame:
    """Return CIC units as a DataFrame."""
    query = """
        SELECT
            id,
            code,
            name,
            complex_name,
            wilaya,
            address,
            manager_name,
            active
        FROM cics
    """

    if active_only:
        query += " WHERE active = 1"

    query += " ORDER BY name"

    with get_db() as conn:
        return pd.read_sql_query(query, conn)


def get_products(active_only: bool = True) -> pd.DataFrame:
    """Return products as a DataFrame."""
    query = """
        SELECT
            id,
            code,
            name,
            category,
            unit,
            minimum_stock,
            active
        FROM products
    """

    if active_only:
        query += " WHERE active = 1"

    query += " ORDER BY name"

    with get_db() as conn:
        return pd.read_sql_query(query, conn)


def get_stock_dataframe(
    cic_id: Optional[int] = None,
    product_id: Optional[int] = None,
) -> pd.DataFrame:
    """Return stock quantities with CIC/product information."""
    query = """
        SELECT
            s.id,
            c.code AS cic_code,
            c.name AS cic_name,
            c.wilaya,
            p.code AS product_code,
            p.name AS product_name,
            p.category,
            p.unit,
            p.minimum_stock,
            s.quantity,
            CASE
                WHEN s.quantity <= p.minimum_stock THEN 'LOW'
                ELSE 'OK'
            END AS stock_status,
            s.updated_at
        FROM stocks s
        INNER JOIN cics c ON c.id = s.cic_id
        INNER JOIN products p ON p.id = s.product_id
        WHERE c.active = 1
          AND p.active = 1
    """

    params = []

    if cic_id is not None:
        query += " AND c.id = ?"
        params.append(cic_id)

    if product_id is not None:
        query += " AND p.id = ?"
        params.append(product_id)

    query += " ORDER BY c.name, p.name"

    with get_db() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_stock_quantity(cic_id: int, product_id: int) -> float:
    """Return current stock for a CIC/product pair."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT quantity
            FROM stocks
            WHERE cic_id = ?
              AND product_id = ?
            """,
            (cic_id, product_id),
        ).fetchone()

    return float(row["quantity"]) if row else 0.0


def update_stock(cic_id: int, product_id: int, quantity: float):
    """Set stock quantity for a CIC/product pair."""
    if quantity < 0:
        raise ValueError("La quantité de stock ne peut pas être négative.")

    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM stocks
            WHERE cic_id = ?
              AND product_id = ?
            """,
            (cic_id, product_id),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE stocks
                SET quantity = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (quantity, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO stocks
                (cic_id, product_id, quantity)
                VALUES (?, ?, ?)
                """,
                (cic_id, product_id, quantity),
            )


def get_transfers(
    user: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
) -> pd.DataFrame:
    """Return transfers according to the user's access scope."""
    query = """
        SELECT
            t.id,
            t.reference,
            t.transfer_type,
            t.source_cic_id,
            sc.name AS source_cic,
            t.destination_cic_id,
            dc.name AS destination_cic,
            p.code AS product_code,
            p.name AS product,
            p.unit,
            t.quantity,
            t.transfer_date,
            t.expected_date,
            t.status,
            t.reason,
            u.full_name AS created_by_name,
            t.approved_at,
            t.received_at,
            t.created_at,
            t.updated_at
        FROM transfers t
        INNER JOIN cics sc ON sc.id = t.source_cic_id
        INNER JOIN cics dc ON dc.id = t.destination_cic_id
        INNER JOIN products p ON p.id = t.product_id
        INNER JOIN users u ON u.id = t.created_by
        WHERE 1 = 1
    """

    params: List[Any] = []

    if user and user["role"] != "Admin":
        query += """
            AND (
                t.source_cic_id = ?
                OR t.destination_cic_id = ?
            )
        """
        params.extend([user["cic_id"], user["cic_id"]])

    if status and status != "Tous":
        query += " AND t.status = ?"
        params.append(status)

    query += " ORDER BY t.id DESC"

    with get_db() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_transfer(transfer_id: int) -> Optional[Dict[str, Any]]:
    """Return one transfer."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM transfers WHERE id = ?",
            (transfer_id,),
        ).fetchone()

    return dict(row) if row else None


def generate_transfer_reference() -> str:
    """Generate a unique transfer reference."""
    with get_db() as conn:
        for _ in range(20):
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            random_part = secrets.token_hex(2).upper()
            reference = f"AGD-{stamp}-{random_part}"

            exists = conn.execute(
                "SELECT id FROM transfers WHERE reference = ?",
                (reference,),
            ).fetchone()

            if not exists:
                return reference

    raise RuntimeError("Impossible de générer une référence unique.")


# =============================================================================
# TRANSFER WORKFLOW
# =============================================================================

def create_transfer(
    transfer_type: str,
    source_cic_id: int,
    destination_cic_id: int,
    product_id: int,
    quantity: float,
    transfer_date: str,
    expected_date: Optional[str],
    reason: str,
    user: Dict[str, Any],
) -> int:
    """Create a transfer request in Pending state."""
    if quantity <= 0:
        raise ValueError("La quantité doit être supérieure à zéro.")

    if source_cic_id == destination_cic_id:
        if transfer_type != "Intra-CIC":
            raise ValueError(
                "Un transfert vers le même CIC doit être de type Intra-CIC."
            )
    else:
        if transfer_type != "Inter-CIC":
            raise ValueError(
                "Un transfert entre CIC différents doit être de type Inter-CIC."
            )

    current_stock = get_stock_quantity(source_cic_id, product_id)

    if quantity > current_stock:
        raise ValueError(
            f"Stock insuffisant. Stock disponible : {current_stock:,.2f}."
        )

    reference = generate_transfer_reference()

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO transfers
            (
                reference,
                transfer_type,
                source_cic_id,
                destination_cic_id,
                product_id,
                quantity,
                transfer_date,
                expected_date,
                status,
                reason,
                created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference,
                transfer_type,
                source_cic_id,
                destination_cic_id,
                product_id,
                quantity,
                transfer_date,
                expected_date,
                STATUS_PENDING,
                reason.strip(),
                user["id"],
            ),
        )

        transfer_id = cursor.lastrowid

        conn.execute(
            """
            INSERT INTO audit_logs
            (
                entity_type,
                entity_id,
                action,
                new_value,
                user_id,
                username
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "TRANSFER",
                transfer_id,
                "CREATE",
                STATUS_PENDING,
                user["id"],
                user["username"],
            ),
        )

    return transfer_id


def update_transfer_status(
    transfer_id: int,
    new_status: str,
    user: Dict[str, Any],
):
    """Apply a controlled status transition to a transfer."""
    transfer = get_transfer(transfer_id)

    if not transfer:
        raise ValueError("Flux introuvable.")

    if not user_can_access_transfer(user, transfer):
        raise PermissionError("Accès refusé à ce flux.")

    old_status = transfer["status"]

    allowed_transitions = {
        STATUS_PENDING: {
            STATUS_APPROVED,
            STATUS_REJECTED,
            STATUS_CANCELLED,
        },
        STATUS_APPROVED: {
            STATUS_IN_TRANSIT,
            STATUS_CANCELLED,
        },
        STATUS_IN_TRANSIT: {
            STATUS_RECEIVED,
            STATUS_CANCELLED,
        },
        STATUS_RECEIVED: {
            STATUS_COMPLETED,
        },
        STATUS_COMPLETED: set(),
        STATUS_REJECTED: set(),
        STATUS_CANCELLED: set(),
    }

    if new_status not in allowed_transitions.get(old_status, set()):
        raise ValueError(
            f"Transition interdite : {old_status} → {new_status}."
        )

    if new_status in (
        STATUS_APPROVED,
        STATUS_REJECTED,
        STATUS_CANCELLED,
    ) and not can_manage_transfers(user):
        raise PermissionError(
            "Seuls Admin et Manager CIC peuvent effectuer cette action."
        )

    if new_status in (STATUS_RECEIVED, STATUS_COMPLETED):
        if user["role"] not in ("Admin", "Manager CIC", "Agent Commercial"):
            raise PermissionError("Rôle non autorisé.")

    # The source stock is reserved when a Pending transfer becomes Approved.
    if old_status == STATUS_PENDING and new_status == STATUS_APPROVED:
        available = get_stock_quantity(
            transfer["source_cic_id"],
            transfer["product_id"],
        )

        if transfer["quantity"] > available:
            raise ValueError(
                "Stock insuffisant au moment de la validation."
            )

        update_stock(
            transfer["source_cic_id"],
            transfer["product_id"],
            available - transfer["quantity"],
        )

    # If an approved transfer is cancelled, return the reserved stock.
    if (
        old_status == STATUS_APPROVED
        and new_status == STATUS_CANCELLED
    ):
        current = get_stock_quantity(
            transfer["source_cic_id"],
            transfer["product_id"],
        )

        update_stock(
            transfer["source_cic_id"],
            transfer["product_id"],
            current + transfer["quantity"],
        )

    # Rejected Pending transfer never reserved stock.
    # Cancellation from Pending therefore changes nothing in inventory.

    # Destination stock is added when the shipment is received.
    if (
        old_status == STATUS_IN_TRANSIT
        and new_status == STATUS_RECEIVED
    ):
        current = get_stock_quantity(
            transfer["destination_cic_id"],
            transfer["product_id"],
        )

        update_stock(
            transfer["destination_cic_id"],
            transfer["product_id"],
            current + transfer["quantity"],
        )

    with get_db() as conn:
        conn.execute(
            """
            UPDATE transfers
            SET
                status = ?,
                approved_by = CASE
                    WHEN ? = 'Approved' THEN ?
                    ELSE approved_by
                END,
                approved_at = CASE
                    WHEN ? = 'Approved' THEN CURRENT_TIMESTAMP
                    ELSE approved_at
                END,
                received_by = CASE
                    WHEN ? IN ('Received', 'Completed') THEN ?
                    ELSE received_by
                END,
                received_at = CASE
                    WHEN ? IN ('Received', 'Completed') THEN CURRENT_TIMESTAMP
                    ELSE received_at
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                new_status,
                new_status,
                user["id"],
                new_status,
                new_status,
                user["id"],
                new_status,
                transfer_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO audit_logs
            (
                entity_type,
                entity_id,
                action,
                old_value,
                new_value,
                user_id,
                username
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "TRANSFER",
                transfer_id,
                "STATUS_CHANGE",
                old_status,
                new_status,
                user["id"],
                user["username"],
            ),
        )


# =============================================================================
# UI HELPERS
# =============================================================================

def apply_css():
    """Apply AGRODIV visual identity."""
    st.markdown(
        f"""
        <style>
        .stApp {{
            background: #F8FAFC;
        }}

        [data-testid="stSidebar"] {{
            background: linear-gradient(
                180deg,
                {AGRODIV_NAVY} 0%,
                #10251A 100%
            );
        }}

        [data-testid="stSidebar"] * {{
            color: #FFFFFF !important;
        }}

        .brand-header {{
            background: linear-gradient(
                135deg,
                {AGRODIV_NAVY},
                {AGRODIV_DARK_GREEN}
            );
            padding: 24px 30px;
            border-radius: 16px;
            color: white;
            margin-bottom: 24px;
            box-shadow: 0 8px 25px rgba(15, 23, 42, 0.12);
        }}

        .brand-header h1 {{
            margin: 0;
            font-size: 27px;
            font-weight: 800;
        }}

        .brand-header p {{
            margin: 7px 0 0 0;
            opacity: 0.88;
        }}

        .metric-card {{
            background: white;
            border: 1px solid {AGRODIV_BORDER};
            border-radius: 14px;
            padding: 18px;
            box-shadow: 0 3px 12px rgba(15, 23, 42, 0.05);
        }}

        .metric-title {{
            color: #64748B;
            font-size: 13px;
            font-weight: 600;
        }}

        .metric-value {{
            color: {AGRODIV_NAVY};
            font-size: 28px;
            font-weight: 800;
            margin-top: 5px;
        }}

        .status-pill {{
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
        }}

        .login-container {{
            max-width: 440px;
            margin: 70px auto;
        }}

        .footer {{
            text-align: center;
            color: #64748B;
            font-size: 12px;
            padding: 30px 0 10px;
        }}

        div[data-testid="stMetric"] {{
            background: white;
            border: 1px solid {AGRODIV_BORDER};
            padding: 12px;
            border-radius: 12px;
        }}

        .stButton > button {{
            border-radius: 8px;
            font-weight: 600;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_header(title: str, subtitle: str = ""):
    """Display the branded page header."""
    subtitle_html = f"<p>{subtitle}</p>" if subtitle else ""

    st.markdown(
        f"""
        <div class="brand-header">
            <h1>{title}</h1>
            {subtitle_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_card(title: str, value: str, icon: str = ""):
    """Display a custom metric card."""
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">{icon} {title}</div>
            <div class="metric-value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def format_number(value: float) -> str:
    """Format numbers in an Algerian-friendly style."""
    return f"{value:,.2f}".replace(",", " ")


def status_badge(status: str) -> str:
    """Return a colored HTML badge."""
    colors = {
        STATUS_PENDING: ("#FEF3C7", "#92400E"),
        STATUS_APPROVED: ("#DBEAFE", "#1D4ED8"),
        STATUS_IN_TRANSIT: ("#E0E7FF", "#4338CA"),
        STATUS_RECEIVED: ("#DCFCE7", "#166534"),
        STATUS_COMPLETED: ("#D1FAE5", "#065F46"),
        STATUS_REJECTED: ("#FEE2E2", "#991B1B"),
        STATUS_CANCELLED: ("#F1F5F9", "#475569"),
    }

    bg, fg = colors.get(status, ("#F1F5F9", "#475569"))

    return (
        f'<span style="background:{bg};color:{fg};'
        f'padding:4px 10px;border-radius:999px;font-weight:700;'
        f'font-size:12px;">{status}</span>'
    )


def show_flash_messages():
    """Display one-time application messages."""
    if "flash_success" in st.session_state:
        st.success(st.session_state.pop("flash_success"))

    if "flash_error" in st.session_state:
        st.error(st.session_state.pop("flash_error"))


def dataframe_download_button(
    dataframe: pd.DataFrame,
    filename: str,
    label: str,
):
    """Create a CSV download button."""
    csv_data = dataframe.to_csv(index=False).encode("utf-8-sig")

    st.download_button(
        label=label,
        data=csv_data,
        file_name=filename,
        mime="text/csv",
        use_container_width=False,
    )


# =============================================================================
# LOGIN
# =============================================================================

def login_page():
    """Render the login page."""
    st.markdown(
        """
        <div class="login-container">
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
        <div class="brand-header">
            <h1>AGRODIV</h1>
            <p>Filiale Céréales Ouest</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("Connexion")
    st.caption("Gestion sécurisée des flux commerciaux intra et inter CIC")

    with st.form("login_form"):
        username = st.text_input(
            "Nom d'utilisateur",
            placeholder="Ex. admin",
        )

        password = st.text_input(
            "Mot de passe",
            type="password",
            placeholder="Votre mot de passe",
        )

        submitted = st.form_submit_button(
            "Se connecter",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if not username or not password:
            st.error("Veuillez renseigner le nom d'utilisateur et le mot de passe.")
        else:
            user = authenticate(username, password)

            if user:
                st.session_state["authenticated"] = True
                st.session_state["user"] = user

                write_audit(
                    entity_type="AUTH",
                    entity_id=user["id"],
                    action="LOGIN",
                    new_value="SUCCESS",
                    user_id=user["id"],
                    username=user["username"],
                )

                st.rerun()
            else:
                st.error("Identifiants invalides.")

    st.info(
        "Comptes de démonstration : admin/admin123 · "
        "manager/manager123 · agent/agent123"
    )

    st.markdown("</div>", unsafe_allow_html=True)


# =============================================================================
# SIDEBAR
# =============================================================================

def render_sidebar(user: Dict[str, Any]) -> str:
    """Render navigation sidebar and return selected page."""
    with st.sidebar:
        st.markdown(
            f"""
            <div style="padding:8px 4px 20px;">
                <div style="font-size:25px;font-weight:900;">
                    AGRODIV
                </div>
                <div style="font-size:12px;opacity:.75;">
                    Filiale Céréales Ouest
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("---")

        st.markdown(f"**Utilisateur**  \n{user['full_name']}")
        st.caption(f"Rôle : {user['role']}")

        if user.get("cic_id"):
            with get_db() as conn:
                cic = conn.execute(
                    "SELECT name FROM cics WHERE id = ?",
                    (user["cic_id"],),
                ).fetchone()

            if cic:
                st.caption(f"CIC : {cic['name']}")

        st.markdown("---")

        pages = [
            "🏠 Tableau de bord",
            "🔄 Flux intra/inter CIC",
            "📦 Stocks & produits",
            "🏢 CIC / Unités",
            "📋 Audit & traçabilité",
        ]

        if user["role"] == "Admin":
            pages.append("👥 Gestion des utilisateurs")

        selected = st.radio(
            "Navigation",
            pages,
            label_visibility="collapsed",
        )

        st.markdown("---")

        if st.button(
            "🚪 Déconnexion",
            use_container_width=True,
        ):
            user_id = user["id"]

            write_audit(
                entity_type="AUTH",
                entity_id=user_id,
                action="LOGOUT",
                new_value="SUCCESS",
                user_id=user_id,
                username=user["username"],
            )

            st.session_state.clear()
            st.rerun()

        st.markdown(
            """
            <div class="footer">
                AGRODIV © 2026<br>
                Plateforme de gestion des flux
            </div>
            """,
            unsafe_allow_html=True,
        )

    return selected


# =============================================================================
# DASHBOARD
# =============================================================================

def dashboard_page(user: Dict[str, Any]):
    """Render the main dashboard."""
    show_header(
        "Tableau de bord",
        "Vue consolidée des stocks et flux commerciaux AGRODIV",
    )

    transfers = get_transfers(user)

    stock = get_stock_dataframe(
        cic_id=None if user["role"] == "Admin" else user["cic_id"]
    )

    total_stock = float(stock["quantity"].sum()) if not stock.empty else 0
    low_stock = (
        int((stock["stock_status"] == "LOW").sum())
        if not stock.empty
        else 0
    )

    pending = (
        int((transfers["status"] == STATUS_PENDING).sum())
        if not transfers.empty
        else 0
    )

    completed = (
        int(
            transfers["status"].isin(
                [STATUS_COMPLETED, STATUS_RECEIVED]
            ).sum()
        )
        if not transfers.empty
        else 0
    )

    total_transfers = len(transfers)

    c1, c2, c3, c4 = st.columns(4)

    with c1:
        metric_card(
            "Stock total",
            f"{format_number(total_stock)} t",
            "📦",
        )

    with c2:
        metric_card(
            "Flux en attente",
            str(pending),
            "⏳",
        )

    with c3:
        metric_card(
            "Flux réalisés",
            str(completed),
            "✅",
        )

    with c4:
        metric_card(
            "Alertes stock",
            str(low_stock),
            "⚠️",
        )

    st.markdown("###")

    left, right = st.columns(2)

    with left:
        st.subheader("Stock par CIC")

        if not stock.empty:
            chart = (
                stock.groupby("cic_name", as_index=True)["quantity"]
                .sum()
                .sort_values(ascending=False)
            )

            st.bar_chart(chart)
        else:
            st.info("Aucune donnée de stock.")

    with right:
        st.subheader("Répartition des flux")

        if not transfers.empty:
            status_counts = (
                transfers["status"]
                .value_counts()
                .rename_axis("Statut")
                .to_frame("Nombre")
            )

            st.bar_chart(status_counts)
        else:
            st.info("Aucun flux enregistré.")

    st.subheader("Statistiques par CIC")

    if not transfers.empty:
        source_stats = (
            transfers.groupby("source_cic")
            .agg(
                Nombre_Flux=("id", "count"),
                Quantite=("quantity", "sum"),
            )
            .reset_index()
            .sort_values("Quantite", ascending=False)
        )

        source_stats["Quantite"] = source_stats["Quantite"].round(2)

        st.dataframe(
            source_stats,
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Derniers mouvements")

    recent = transfers.head(10).copy()

    if not recent.empty:
        recent_display = recent[
            [
                "reference",
                "transfer_type",
                "source_cic",
                "destination_cic",
                "product",
                "quantity",
                "status",
                "created_at",
            ]
        ]

        st.dataframe(
            recent_display,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Aucun mouvement récent.")


# =============================================================================
# TRANSFER PAGE
# =============================================================================

def transfer_creation_form(user: Dict[str, Any]):
    """Render transfer creation form."""
    st.subheader("Créer une demande de flux")

    cics = get_cics()
    products = get_products()

    if cics.empty or products.empty:
        st.warning("Veuillez d'abord configurer les CIC et produits.")
        return

    cic_options = {
        row["name"]: int(row["id"])
        for _, row in cics.iterrows()
    }

    product_options = {
        f"{row['code']} — {row['name']}": int(row["id"])
        for _, row in products.iterrows()
    }

    default_source_index = 0

    if user.get("cic_id"):
        source_matches = [
            i
            for i, value in enumerate(cic_options.values())
            if value == user["cic_id"]
        ]

        if source_matches:
            default_source_index = source_matches[0]

    with st.form("create_transfer"):
        transfer_type = st.radio(
            "Type de flux",
            ["Intra-CIC", "Inter-CIC"],
            horizontal=True,
        )

        col1, col2 = st.columns(2)

        with col1:
            source_name = st.selectbox(
                "CIC source",
                list(cic_options.keys()),
                index=default_source_index,
            )

        with col2:
            destination_name = st.selectbox(
                "CIC destination",
                list(cic_options.keys()),
            )

        product_name = st.selectbox(
            "Produit",
            list(product_options.keys()),
        )

        quantity = st.number_input(
            "Quantité",
            min_value=0.01,
            value=100.0,
            step=10.0,
        )

        col3, col4 = st.columns(2)

        with col3:
            transfer_date = st.date_input(
                "Date du flux",
                value=date.today(),
            )

        with col4:
            expected_date = st.date_input(
                "Date prévue de réception",
                value=date.today(),
            )

        reason = st.text_area(
            "Motif / observations",
            placeholder="Ex. Rééquilibrage de stock, demande commerciale...",
        )

        submitted = st.form_submit_button(
            "Créer la demande",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        source_id = cic_options[source_name]
        destination_id = cic_options[destination_name]
        product_id = product_options[product_name]

        if user["role"] != "Admin":
            if source_id != user.get("cic_id"):
                st.error(
                    "Vous ne pouvez créer un flux qu'à partir de votre CIC."
                )
                return

        try:
            transfer_id = create_transfer(
                transfer_type=transfer_type,
                source_cic_id=source_id,
                destination_cic_id=destination_id,
                product_id=product_id,
                quantity=float(quantity),
                transfer_date=transfer_date.isoformat(),
                expected_date=expected_date.isoformat(),
                reason=reason,
                user=user,
            )

            st.success(
                f"Demande créée avec succès. ID du flux : {transfer_id}"
            )
            st.rerun()

        except Exception as exc:
            st.error(str(exc))


def transfer_workflow_panel(user: Dict[str, Any]):
    """Render transfer workflow controls."""
    st.subheader("Validation et suivi des flux")

    transfers = get_transfers(user)

    if transfers.empty:
        st.info("Aucun flux disponible.")
        return

    display = transfers[
        [
            "id",
            "reference",
            "transfer_type",
            "source_cic",
            "destination_cic",
            "product",
            "quantity",
            "transfer_date",
            "status",
        ]
    ].copy()

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("#### Action sur un flux")

    reference_options = {
        f"{row.reference} — {row.product} — {row.status}": int(row.id)
        for row in transfers.itertuples()
    }

    selected_label = st.selectbox(
        "Sélectionner un flux",
        list(reference_options.keys()),
    )

    transfer_id = reference_options[selected_label]
    transfer = get_transfer(transfer_id)

    if not transfer:
        st.error("Flux introuvable.")
        return

    st.markdown(
        f"""
        **Référence :** `{transfer['reference']}`  
        **Statut actuel :** {status_badge(transfer['status'])}
        """,
        unsafe_allow_html=True,
    )

    current = transfer["status"]

    transitions = {
        STATUS_PENDING: [
            STATUS_APPROVED,
            STATUS_REJECTED,
            STATUS_CANCELLED,
        ],
        STATUS_APPROVED: [
            STATUS_IN_TRANSIT,
            STATUS_CANCELLED,
        ],
        STATUS_IN_TRANSIT: [
            STATUS_RECEIVED,
            STATUS_CANCELLED,
        ],
        STATUS_RECEIVED: [
            STATUS_COMPLETED,
        ],
    }

    available = transitions.get(current, [])

    if not available:
        st.info("Aucune action disponible pour ce statut.")
        return

    if user["role"] == "Agent Commercial":
        available = [
            x
            for x in available
            if x in (STATUS_RECEIVED, STATUS_COMPLETED)
        ]

    if not available:
        st.warning(
            "Votre rôle ne dispose d'aucune action disponible sur ce flux."
        )
        return

    new_status = st.selectbox(
        "Nouveau statut",
        available,
    )

    if st.button(
        "Appliquer la transition",
        type="primary",
        use_container_width=True,
    ):
        try:
            update_transfer_status(
                transfer_id,
                new_status,
                user,
            )

            st.success(
                f"Le flux {transfer['reference']} est maintenant "
                f"« {new_status} »."
            )
            st.rerun()

        except Exception as exc:
            st.error(str(exc))


def transfer_reports(user: Dict[str, Any]):
    """Render transfer reports and CSV export."""
    st.subheader("Rapports des flux")

    transfers = get_transfers(user)

    if transfers.empty:
        st.info("Aucun transfert à exporter.")
        return

    c1, c2, c3 = st.columns(3)

    with c1:
        status_filter = st.selectbox(
            "Filtrer par statut",
            ["Tous"] + ALL_STATUSES,
        )

    with c2:
        type_filter = st.selectbox(
            "Type",
            ["Tous", "Intra-CIC", "Inter-CIC"],
        )

    with c3:
        product_filter = st.selectbox(
            "Produit",
            ["Tous"] + sorted(transfers["product"].unique().tolist()),
        )

    filtered = transfers.copy()

    if status_filter != "Tous":
        filtered = filtered[
            filtered["status"] == status_filter
        ]

    if type_filter != "Tous":
        filtered = filtered[
            filtered["transfer_type"] == type_filter
        ]

    if product_filter != "Tous":
        filtered = filtered[
            filtered["product"] == product_filter
        ]

    st.write(f"**{len(filtered)} flux trouvé(s)**")

    st.dataframe(
        filtered,
        use_container_width=True,
        hide_index=True,
    )

    dataframe_download_button(
        filtered,
        f"agrodiv_flux_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        "⬇️ Exporter le rapport CSV",
    )


def transfers_page(user: Dict[str, Any]):
    """Render the transfer module."""
    show_header(
        "Flux intra & inter CIC",
        "Création, validation, suivi et reporting des mouvements commerciaux",
    )

    tab1, tab2, tab3 = st.tabs(
        [
            "➕ Nouvelle demande",
            "🔄 Workflow",
            "📊 Rapports",
        ]
    )

    with tab1:
        transfer_creation_form(user)

    with tab2:
        transfer_workflow_panel(user)

    with tab3:
        transfer_reports(user)


# =============================================================================
# STOCKS & PRODUCTS
# =============================================================================

def products_management(user: Dict[str, Any]):
    """Render product management."""
    st.subheader("Produits")

    products = get_products(active_only=False)

    if not products.empty:
        st.dataframe(
            products,
            use_container_width=True,
            hide_index=True,
        )

    if can_manage_products(user):
        with st.expander("➕ Ajouter un produit"):
            with st.form("add_product"):
                code = st.text_input("Code produit")
                name = st.text_input("Désignation")
                category = st.selectbox(
                    "Catégorie",
                    [
                        "Céréales",
                        "Produits finis",
                        "Sous-produits",
                        "Autres",
                    ],
                )
                unit = st.selectbox(
                    "Unité",
                    ["Tonnes", "Kg", "Unités", "Sacs"],
                )
                minimum_stock = st.number_input(
                    "Seuil minimum",
                    min_value=0.0,
                    value=50.0,
                )

                submitted = st.form_submit_button(
                    "Ajouter",
                    type="primary",
                )

            if submitted:
                if not code.strip() or not name.strip():
                    st.error("Code et désignation sont obligatoires.")
                else:
                    try:
                        with get_db() as conn:
                            cursor = conn.execute(
                                """
                                INSERT INTO products
                                (code, name, category, unit, minimum_stock)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (
                                    code.strip().upper(),
                                    name.strip(),
                                    category,
                                    unit,
                                    minimum_stock,
                                ),
                            )

                            product_id = cursor.lastrowid

                        write_audit(
                            "PRODUCT",
                            product_id,
                            "CREATE",
                            new_value=name,
                            user_id=user["id"],
                            username=user["username"],
                        )

                        st.success("Produit ajouté.")
                        st.rerun()

                    except sqlite3.IntegrityError:
                        st.error("Ce code produit existe déjà.")

        with st.expander("✏️ Modifier un produit"):
            if products.empty:
                st.info("Aucun produit.")
            else:
                product_map = {
                    f"{r.code} — {r.name}": int(r.id)
                    for r in products.itertuples()
                    if int(r.active) == 1
                }

                if product_map:
                    selected = st.selectbox(
                        "Produit",
                        list(product_map.keys()),
                    )

                    selected_id = product_map[selected]
                    selected_row = products[
                        products["id"] == selected_id
                    ].iloc[0]

                    with st.form("edit_product"):
                        new_name = st.text_input(
                            "Désignation",
                            value=selected_row["name"],
                        )

                        new_category = st.selectbox(
                            "Catégorie",
                            [
                                "Céréales",
                                "Produits finis",
                                "Sous-produits",
                                "Autres",
                            ],
                            index=[
                                "Céréales",
                                "Produits finis",
                                "Sous-produits",
                                "Autres",
                            ].index(selected_row["category"]),
                        )

                        new_minimum = st.number_input(
                            "Seuil minimum",
                            min_value=0.0,
                            value=float(
                                selected_row["minimum_stock"]
                            ),
                        )

                        save = st.form_submit_button(
                            "Enregistrer",
                            type="primary",
                        )

                    if save:
                        with get_db() as conn:
                            conn.execute(
                                """
                                UPDATE products
                                SET name = ?,
                                    category = ?,
                                    minimum_stock = ?
                                WHERE id = ?
                                """,
                                (
                                    new_name.strip(),
                                    new_category,
                                    new_minimum,
                                    selected_id,
                                ),
                            )

                        write_audit(
                            "PRODUCT",
                            selected_id,
                            "UPDATE",
                            new_value=new_name,
                            user_id=user["id"],
                            username=user["username"],
                        )

                        st.success("Produit modifié.")
                        st.rerun()


def stock_management(user: Dict[str, Any]):
    """Render stock management."""
    st.subheader("Stocks par CIC")

    cics = get_cics()
    products = get_products()

    if cics.empty or products.empty:
        st.warning("Données CIC/produits insuffisantes.")
        return

    cic_filter_options = {
        "Tous les CIC": None,
        **{
            row["name"]: int(row["id"])
            for _, row in cics.iterrows()
        },
    }

    product_filter_options = {
        "Tous les produits": None,
        **{
            f"{row['code']} — {row['name']}": int(row["id"])
            for _, row in products.iterrows()
        },
    }

    col1, col2 = st.columns(2)

    with col1:
        selected_cic = st.selectbox(
            "CIC",
            list(cic_filter_options.keys()),
        )

    with col2:
        selected_product = st.selectbox(
            "Produit",
            list(product_filter_options.keys()),
        )

    stock = get_stock_dataframe(
        cic_id=cic_filter_options[selected_cic],
        product_id=product_filter_options[selected_product],
    )

    if stock.empty:
        st.info("Aucun stock.")
    else:
        st.dataframe(
            stock,
            use_container_width=True,
            hide_index=True,
        )

        low_stock = stock[stock["stock_status"] == "LOW"]

        if not low_stock.empty:
            st.warning(
                f"{len(low_stock)} ligne(s) de stock sous le seuil minimum."
            )

    if can_manage_products(user):
        st.markdown("### Ajustement manuel du stock")

        cic_options = {
            row["name"]: int(row["id"])
            for _, row in cics.iterrows()
        }

        product_options = {
            f"{row['code']} — {row['name']}": int(row["id"])
            for _, row in products.iterrows()
        }

        with st.form("stock_adjustment"):
            cic_name = st.selectbox(
                "CIC",
                list(cic_options.keys()),
            )

            product_name = st.selectbox(
                "Produit",
                list(product_options.keys()),
            )

            current_quantity = get_stock_quantity(
                cic_options[cic_name],
                product_options[product_name],
            )

            st.info(
                f"Stock actuel : {format_number(current_quantity)}"
            )

            new_quantity = st.number_input(
                "Nouveau stock",
                min_value=0.0,
                value=float(current_quantity),
                step=10.0,
            )

            save = st.form_submit_button(
                "Enregistrer le stock",
                type="primary",
            )

        if save:
            old_quantity = current_quantity

            update_stock(
                cic_options[cic_name],
                product_options[product_name],
                float(new_quantity),
            )

            write_audit(
                "STOCK",
                None,
                "MANUAL_ADJUSTMENT",
                old_value=str(old_quantity),
                new_value=str(new_quantity),
                user_id=user["id"],
                username=user["username"],
            )

            st.success("Stock mis à jour.")
            st.rerun()


def stocks_page(user: Dict[str, Any]):
    """Render stocks and products module."""
    show_header(
        "Stocks & produits",
        "Gestion des références, niveaux de stock et seuils d'alerte",
    )

    tab1, tab2 = st.tabs(
        [
            "📦 Stocks",
            "🌾 Produits",
        ]
    )

    with tab1:
        stock_management(user)

    with tab2:
        products_management(user)


# =============================================================================
# CIC MANAGEMENT
# =============================================================================

def cics_page(user: Dict[str, Any]):
    """Render CIC management."""
    show_header(
        "Unités CIC",
        "Référentiel des centres et complexes commerciaux AGRODIV",
    )

    cics = get_cics(active_only=False)

    st.dataframe(
        cics,
        use_container_width=True,
        hide_index=True,
    )

    if user["role"] == "Admin":
        st.subheader("Ajouter un CIC")

        with st.form("add_cic"):
            col1, col2 = st.columns(2)

            with col1:
                code = st.text_input("Code CIC")
                name = st.text_input("Nom")

            with col2:
                complex_name = st.text_input("Complexe")
                wilaya = st.text_input("Wilaya")

            address = st.text_input("Adresse")
            manager_name = st.text_input("Responsable")

            submitted = st.form_submit_button(
                "Ajouter le CIC",
                type="primary",
            )

        if submitted:
            if not code.strip() or not name.strip():
                st.error("Le code et le nom sont obligatoires.")
                return

            try:
                with get_db() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO cics
                        (
                            code,
                            name,
                            complex_name,
                            wilaya,
                            address,
                            manager_name
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            code.strip().upper(),
                            name.strip(),
                            complex_name.strip(),
                            wilaya.strip(),
                            address.strip(),
                            manager_name.strip(),
                        ),
                    )

                    cic_id = cursor.lastrowid

                write_audit(
                    "CIC",
                    cic_id,
                    "CREATE",
                    new_value=name,
                    user_id=user["id"],
                    username=user["username"],
                )

                st.success("CIC ajouté.")
                st.rerun()

            except sqlite3.IntegrityError:
                st.error("Ce code CIC existe déjà.")


# =============================================================================
# AUDIT
# =============================================================================

def audit_page(user: Dict[str, Any]):
    """Render the audit trail."""
    show_header(
        "Traçabilité & audit",
        "Journal des opérations et changements de statut",
    )

    query = """
        SELECT
            id,
            created_at,
            entity_type,
            entity_id,
            action,
            old_value,
            new_value,
            username
        FROM audit_logs
    """

    params = []

    if user["role"] != "Admin":
        query += " WHERE user_id = ?"
        params.append(user["id"])

    query += " ORDER BY id DESC LIMIT 2000"

    with get_db() as conn:
        logs = pd.read_sql_query(
            query,
            conn,
            params=params,
        )

    if logs.empty:
        st.info("Aucun événement d'audit.")
        return

    st.metric("Événements affichés", len(logs))

    st.dataframe(
        logs,
        use_container_width=True,
        hide_index=True,
    )

    dataframe_download_button(
        logs,
        f"agrodiv_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        "⬇️ Exporter l'audit CSV",
    )


# =============================================================================
# USER MANAGEMENT
# =============================================================================

def users_page(user: Dict[str, Any]):
    """Render administrator-only user management."""
    show_header(
        "Gestion des utilisateurs",
        "Administration des comptes, rôles et rattachements CIC",
    )

    with get_db() as conn:
        users = pd.read_sql_query(
            """
            SELECT
                u.id,
                u.username,
                u.full_name,
                u.role,
                c.name AS cic,
                u.active,
                u.created_at
            FROM users u
            LEFT JOIN cics c ON c.id = u.cic_id
            ORDER BY u.id
            """,
            conn,
        )

    st.dataframe(
        users,
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Créer un utilisateur")

    cics = get_cics()

    cic_options = {
        "Aucun CIC": None,
        **{
            row["name"]: int(row["id"])
            for _, row in cics.iterrows()
        },
    }

    with st.form("new_user"):
        username = st.text_input("Nom d'utilisateur")
        full_name = st.text_input("Nom complet")

        role = st.selectbox(
            "Rôle",
            ROLES,
        )

        password = st.text_input(
            "Mot de passe initial",
            type="password",
        )

        cic_name = st.selectbox(
            "CIC de rattachement",
            list(cic_options.keys()),
        )

        active = st.checkbox(
            "Compte actif",
            value=True,
        )

        submitted = st.form_submit_button(
            "Créer le compte",
            type="primary",
        )

    if submitted:
        if not username.strip() or not full_name.strip():
            st.error("Nom d'utilisateur et nom complet obligatoires.")
            return

        if len(password) < 6:
            st.error("Le mot de passe doit contenir au moins 6 caractères.")
            return

        try:
            with get_db() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users
                    (
                        username,
                        full_name,
                        password_hash,
                        role,
                        cic_id,
                        active
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username.strip(),
                        full_name.strip(),
                        hash_password(password),
                        role,
                        cic_options[cic_name],
                        1 if active else 0,
                    ),
                )

                user_id = cursor.lastrowid

            write_audit(
                "USER",
                user_id,
                "CREATE",
                new_value=username,
                user_id=user["id"],
                username=user["username"],
            )

            st.success("Utilisateur créé.")
            st.rerun()

        except sqlite3.IntegrityError:
            st.error("Ce nom d'utilisateur existe déjà.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Application entry point."""
    st.set_page_config(
        page_title="AGRODIV — Flux commerciaux CIC",
        page_icon="🌾",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    apply_css()
    init_database()

    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    show_flash_messages()

    if not st.session_state["authenticated"]:
        login_page()
        return

    user = st.session_state.get("user")

    if not user:
        st.session_state.clear()
        st.rerun()
        return

    selected_page = render_sidebar(user)

    if selected_page == "🏠 Tableau de bord":
        dashboard_page(user)

    elif selected_page == "🔄 Flux intra/inter CIC":
        transfers_page(user)

    elif selected_page == "📦 Stocks & produits":
        stocks_page(user)

    elif selected_page == "🏢 CIC / Unités":
        cics_page(user)

    elif selected_page == "📋 Audit & traçabilité":
        audit_page(user)

    elif selected_page == "👥 Gestion des utilisateurs":
        if user["role"] == "Admin":
            users_page(user)
        else:
            st.error("Accès refusé.")

    else:
        dashboard_page(user)


if __name__ == "__main__":
    main()