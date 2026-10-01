"""
AGRODIV — Plateforme de gestion des flux commerciaux intra et inter CIC
Filiale Céréales Ouest (FCO)

Single-file Streamlit + SQLite application (version dépôts).

Installation:
    pip install streamlit pandas openpyxl

Run:
    streamlit run app.py

Comptes de démonstration:
    admin        / admin123     (Admin : tous les droits)
    manager      / manager123   (Manager CIC SBA : consultation uniquement)
    manager_oran / manager123   (Manager CIC Oran : consultation uniquement)
    agent        / agent123     (Chef de dépôt SBA-01)
    agent2       / agent123     (Chef de dépôt SBA-02)
    agent_oran   / agent123     (Chef de dépôt ORN-01)

Règles métier :
    - Un CIC contient des dépôts. Les transferts se font de dépôt à dépôt.
    - Agent Commercial = chef de dépôt : ne voit que les produits de son dépôt.
        * il valide la SORTIE (Pending -> In Transit) : le stock source est déduit
        * le chef du dépôt destinataire valide la RÉCEPTION (Received) : son stock
          est mis à jour automatiquement, puis Completed.
        * il peut mettre à jour le stock de son dépôt.
    - Manager CIC : consultation uniquement, limitée aux dépôts de son CIC.
    - Admin : tous les droits (y compris les seuils de stock bas).
"""

import base64
import hashlib
import io
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from functools import lru_cache
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import streamlit as st


# =============================================================================
# CONFIGURATION
# =============================================================================

APP_TITLE = "Plateforme de gestion des flux commerciaux et inter CIC"
COMPANY_NAME = "AGRODIV — Filiale Céréales Ouest"
# Nouveau fichier : le schéma a changé (dépôts). L'ancienne base n'est pas touchée.
DB_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "agrodiv_flux_v2.db"
)

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.jpg")

ROLE_ADMIN = "Admin"
ROLE_MANAGER = "Manager CIC"
ROLE_AGENT = "Agent Commercial"
ROLES = [ROLE_ADMIN, ROLE_MANAGER, ROLE_AGENT]

STATUS_PENDING = "Pending"
STATUS_IN_TRANSIT = "In Transit"
STATUS_RECEIVED = "Received"
STATUS_COMPLETED = "Completed"
STATUS_CANCELLED = "Cancelled"

ALL_STATUSES = [
    STATUS_PENDING,
    STATUS_IN_TRANSIT,
    STATUS_RECEIVED,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
]

TRANSITIONS = {
    STATUS_PENDING: [STATUS_IN_TRANSIT, STATUS_CANCELLED],
    STATUS_IN_TRANSIT: [STATUS_RECEIVED, STATUS_CANCELLED],
    STATUS_RECEIVED: [STATUS_COMPLETED],
    STATUS_COMPLETED: [],
    STATUS_CANCELLED: [],
}

PAGE_DASH = "🏠 Tableau de bord"
PAGE_FLUX = "🔄 Flux inter-dépôts"
PAGE_STOCK = "📦 Stocks & produits"
PAGE_CIC = "🏢 CIC & Dépôts"
PAGE_AUDIT = "📋 Audit & traçabilité"
PAGE_USERS = "👥 Gestion des utilisateurs"
PAGE_PROFILE = "🔑 Mon mot de passe"

PRODUCT_CATEGORIES = ["Céréales", "Produits finis", "Sous-produits", "Autres"]
PRODUCT_UNITS = ["QX", "Kg", "Unités", "Sacs"]

AGRODIV_GREEN = "#15803D"
AGRODIV_DARK_GREEN = "#166534"
AGRODIV_NAVY = "#0F172A"
AGRODIV_BORDER = "#D1D5DB"


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
    """Create all tables; insert demo data only on the very first run."""
    with get_db() as conn:
        conn.executescript(
            """
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

            CREATE TABLE IF NOT EXISTS depots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                cic_id INTEGER NOT NULL,
                address TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(cic_id) REFERENCES cics(id)
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN (
                    'Admin', 'Manager CIC', 'Agent Commercial'
                )),
                cic_id INTEGER,
                depot_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(cic_id) REFERENCES cics(id),
                FOREIGN KEY(depot_id) REFERENCES depots(id)
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'QX',
                minimum_stock REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                depot_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity REAL NOT NULL DEFAULT 0 CHECK(quantity >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(depot_id, product_id),
                FOREIGN KEY(depot_id) REFERENCES depots(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT UNIQUE NOT NULL,
                transfer_type TEXT NOT NULL CHECK(
                    transfer_type IN ('Intra-CIC', 'Inter-CIC')
                ),
                source_depot_id INTEGER NOT NULL,
                destination_depot_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity REAL NOT NULL CHECK(quantity > 0),
                transfer_date TEXT NOT NULL,
                expected_date TEXT,
                status TEXT NOT NULL DEFAULT 'Pending',
                reason TEXT,
                truck_plate TEXT NOT NULL,
                driver_name TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                shipped_by INTEGER,
                shipped_at TEXT,
                received_by INTEGER,
                received_at TEXT,
                completed_by INTEGER,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(source_depot_id <> destination_depot_id),
                FOREIGN KEY(source_depot_id) REFERENCES depots(id),
                FOREIGN KEY(destination_depot_id) REFERENCES depots(id),
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(shipped_by) REFERENCES users(id),
                FOREIGN KEY(received_by) REFERENCES users(id),
                FOREIGN KEY(completed_by) REFERENCES users(id)
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
                ON transfers(source_depot_id);
            CREATE INDEX IF NOT EXISTS idx_transfers_destination
                ON transfers(destination_depot_id);
            CREATE INDEX IF NOT EXISTS idx_audit_entity
                ON audit_logs(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_stocks_depot_product
                ON stocks(depot_id, product_id);
            CREATE INDEX IF NOT EXISTS idx_depots_cic
                ON depots(cic_id);
            """
        )

        # Données de démonstration : uniquement si la base est vide, pour que
        # les éléments supprimés par l'admin ne réapparaissent pas.
        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            seed_demo_data(conn)


def seed_demo_data(conn: sqlite3.Connection):
    """Insert CICs, depots, products, stocks and demo users."""
    cics = [
        ("CIC-SBA", "CIC Sidi Bel Abbès", "Complexe Sidi Bel Abbès",
         "Sidi Bel Abbès", "63 Avenue Aissat Idir", "Direction CIC SBA"),
        ("CIC-ORN", "CIC Oran", "Complexe Oran",
         "Oran", "Zone Industrielle Oran", "Direction CIC Oran"),
        ("CIC-BEC", "CIC Béchar", "Complexe Béchar",
         "Béchar", "Zone Industrielle Béchar", "Direction CIC Béchar"),
        ("CIC-TLM", "CIC Tlemcen", "Complexe Tlemcen",
         "Tlemcen", "Zone Industrielle Tlemcen", "Direction CIC Tlemcen"),
    ]
    for row in cics:
        conn.execute(
            """
            INSERT INTO cics
            (code, name, complex_name, wilaya, address, manager_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    cic_ids = {
        r["code"]: r["id"]
        for r in conn.execute("SELECT id, code FROM cics").fetchall()
    }

    depots = [
        ("DEP-SBA-01", "Dépôt Central SBA", "CIC-SBA", "Zone centrale SBA"),
        ("DEP-SBA-02", "Dépôt Sud SBA", "CIC-SBA", "Zone sud SBA"),
        ("DEP-ORN-01", "Dépôt Central Oran", "CIC-ORN", "Zone industrielle Oran"),
        ("DEP-ORN-02", "Dépôt Port Oran", "CIC-ORN", "Port d'Oran"),
        ("DEP-BEC-01", "Dépôt Béchar", "CIC-BEC", "Zone industrielle Béchar"),
        ("DEP-TLM-01", "Dépôt Tlemcen", "CIC-TLM", "Zone industrielle Tlemcen"),
    ]
    for code, name, cic_code, address in depots:
        conn.execute(
            "INSERT INTO depots (code, name, cic_id, address) VALUES (?, ?, ?, ?)",
            (code, name, cic_ids[cic_code], address),
        )

    depot_ids = {
        r["code"]: r["id"]
        for r in conn.execute("SELECT id, code FROM depots").fetchall()
    }

    products = [
        ("BT-001", "Blé Tendre", "Céréales", "QX", 100),
        ("BD-001", "Blé Dur", "Céréales", "QX", 100),
        ("FAR-001", "Farine", "Produits finis", "QX", 50),
        ("SEM-001", "Semoule", "Produits finis", "QX", 50),
        ("SON-001", "Son de blé", "Sous-produits", "QX", 25),
    ]
    for row in products:
        conn.execute(
            """
            INSERT INTO products (code, name, category, unit, minimum_stock)
            VALUES (?, ?, ?, ?, ?)
            """,
            row,
        )

    product_ids = {
        r["code"]: r["id"]
        for r in conn.execute("SELECT id, code FROM products").fetchall()
    }

    stock_values = {
        "DEP-SBA-01": {"BT-001": 1100, "BD-001": 520, "FAR-001": 380,
                       "SEM-001": 250, "SON-001": 80},
        "DEP-SBA-02": {"BT-001": 750, "BD-001": 400, "FAR-001": 240,
                       "SEM-001": 200, "SON-001": 50},
        "DEP-ORN-01": {"BT-001": 800, "BD-001": 420, "FAR-001": 260,
                       "SEM-001": 200, "SON-001": 55},
        "DEP-ORN-02": {"BT-001": 450, "BD-001": 250, "FAR-001": 160,
                       "SEM-001": 130, "SON-001": 35},
        "DEP-BEC-01": {"BT-001": 780, "BD-001": 310, "FAR-001": 210,
                       "SEM-001": 170, "SON-001": 55},
        "DEP-TLM-01": {"BT-001": 920, "BD-001": 480, "FAR-001": 275,
                       "SEM-001": 230, "SON-001": 75},
    }
    for depot_code, items in stock_values.items():
        for product_code, qty in items.items():
            conn.execute(
                "INSERT INTO stocks (depot_id, product_id, quantity) VALUES (?, ?, ?)",
                (depot_ids[depot_code], product_ids[product_code], qty),
            )

    users = [
        ("admin", "Administrateur AGRODIV", "admin123", ROLE_ADMIN,
         None, None),
        ("manager", "Manager CIC Sidi Bel Abbès", "manager123", ROLE_MANAGER,
         cic_ids["CIC-SBA"], None),
        ("manager_oran", "Manager CIC Oran", "manager123", ROLE_MANAGER,
         cic_ids["CIC-ORN"], None),
        ("agent", "Chef dépôt Central SBA", "agent123", ROLE_AGENT,
         cic_ids["CIC-SBA"], depot_ids["DEP-SBA-01"]),
        ("agent2", "Chef dépôt Sud SBA", "agent123", ROLE_AGENT,
         cic_ids["CIC-SBA"], depot_ids["DEP-SBA-02"]),
        ("agent_oran", "Chef dépôt Central Oran", "agent123", ROLE_AGENT,
         cic_ids["CIC-ORN"], depot_ids["DEP-ORN-01"]),
    ]
    for username, full_name, pwd, role, cic_id, depot_id in users:
        conn.execute(
            """
            INSERT INTO users
            (username, full_name, password_hash, role, cic_id, depot_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, full_name, hash_password(pwd), role, cic_id, depot_id),
        )


# =============================================================================
# AUDIT
# =============================================================================

def _audit(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    old_value: Optional[str],
    new_value: Optional[str],
    user: Optional[Dict[str, Any]] = None,
):
    """Insert an audit record using an existing connection (same transaction)."""
    conn.execute(
        """
        INSERT INTO audit_logs
        (entity_type, entity_id, action, old_value, new_value, user_id, username)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            entity_id,
            action,
            old_value,
            new_value,
            user["id"] if user else None,
            user["username"] if user else None,
        ),
    )


def write_audit(
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
):
    """Write an audit record."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs
            (entity_type, entity_id, action, old_value, new_value, user_id, username)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, action, old_value, new_value,
             user_id, username),
        )


# =============================================================================
# AUTHENTICATION, SCOPE & PERMISSIONS
# =============================================================================

def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticate a user and return the user record."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1",
            (username.strip(),),
        ).fetchone()

    if row and verify_password(password, row["password_hash"]):
        return dict(row)
    return None


def get_user(user_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Reload an active user from the database."""
    if user_id is None:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def is_admin(user: Dict[str, Any]) -> bool:
    return user["role"] == ROLE_ADMIN


def can_manage_products(user: Dict[str, Any]) -> bool:
    """Création / modification / suppression des produits + seuils : Admin."""
    return is_admin(user)


def can_create_transfer(user: Dict[str, Any]) -> bool:
    return user["role"] in (ROLE_ADMIN, ROLE_AGENT)


def can_adjust_stock(user: Dict[str, Any]) -> bool:
    return user["role"] in (ROLE_ADMIN, ROLE_AGENT)


def get_scope_depot_ids(user: Dict[str, Any]) -> Optional[List[int]]:
    """
    Depots visible by the user.
    None  -> all depots (Admin)
    list  -> Manager: depots of his CIC / Agent: his own depot
    """
    if user["role"] == ROLE_ADMIN:
        return None

    if user["role"] == ROLE_MANAGER:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id FROM depots WHERE cic_id = ?",
                (user.get("cic_id"),),
            ).fetchall()
        return [int(r["id"]) for r in rows]

    depot_id = user.get("depot_id")
    return [int(depot_id)] if depot_id else []


def in_clause(ids: List[int]) -> Tuple[str, List[int]]:
    """Build a safe SQL IN (...) clause."""
    if not ids:
        return "(NULL)", []
    return "(" + ",".join("?" * len(ids)) + ")", [int(i) for i in ids]


def available_actions(user: Dict[str, Any], transfer: Dict[str, Any]) -> List[str]:
    """
    Status transitions the user may apply to a transfer.

    Admin   : toutes les transitions du workflow.
    Agent   : dépôt SOURCE  -> valider la sortie (In Transit) / annuler (Pending)
              dépôt DESTINATION -> Received puis Completed
    Manager : aucune (consultation).
    """
    status = transfer["status"]
    allowed = TRANSITIONS.get(status, [])

    if user["role"] == ROLE_ADMIN:
        return list(allowed)

    if user["role"] != ROLE_AGENT:
        return []

    depot_id = user.get("depot_id")
    if not depot_id:
        return []

    actions: List[str] = []

    if depot_id == transfer["source_depot_id"] and status == STATUS_PENDING:
        actions = [STATUS_IN_TRANSIT, STATUS_CANCELLED]

    elif depot_id == transfer["destination_depot_id"]:
        if status == STATUS_IN_TRANSIT:
            actions = [STATUS_RECEIVED]
        elif status == STATUS_RECEIVED:
            actions = [STATUS_COMPLETED]

    return [a for a in actions if a in allowed]


# =============================================================================
# DATA ACCESS
# =============================================================================

def get_cics(active_only: bool = True) -> pd.DataFrame:
    query = """
        SELECT id, code, name, complex_name, wilaya, address,
               manager_name, active
        FROM cics
    """
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name"

    with get_db() as conn:
        return pd.read_sql_query(query, conn)


def get_depots(
    user: Optional[Dict[str, Any]] = None,
    active_only: bool = True,
) -> pd.DataFrame:
    """Return depots (with CIC name and chef de dépôt), scoped to the user."""
    query = """
        SELECT
            d.id,
            d.code,
            d.name,
            d.cic_id,
            c.name AS cic_name,
            d.address,
            (
                SELECT group_concat(u.full_name, ', ')
                FROM users u
                WHERE u.depot_id = d.id
                  AND u.role = 'Agent Commercial'
                  AND u.active = 1
            ) AS chef_depot,
            d.active
        FROM depots d
        INNER JOIN cics c ON c.id = d.cic_id
        WHERE 1 = 1
    """
    params: List[Any] = []

    if active_only:
        query += " AND d.active = 1"

    if user is not None:
        ids = get_scope_depot_ids(user)
        if ids is not None:
            clause, p = in_clause(ids)
            query += f" AND d.id IN {clause}"
            params.extend(p)

    query += " ORDER BY c.name, d.name"

    with get_db() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_products(
    user: Optional[Dict[str, Any]] = None,
    active_only: bool = True,
) -> pd.DataFrame:
    """
    Return products. For Manager / Agent only the products that exist in the
    depots they are allowed to see are returned.
    """
    query = """
        SELECT id, code, name, category, unit, minimum_stock, active
        FROM products
        WHERE 1 = 1
    """
    params: List[Any] = []

    if active_only:
        query += " AND active = 1"

    if user is not None:
        ids = get_scope_depot_ids(user)
        if ids is not None:
            clause, p = in_clause(ids)
            query += (
                f" AND id IN (SELECT product_id FROM stocks "
                f"WHERE depot_id IN {clause})"
            )
            params.extend(p)

    query += " ORDER BY name"

    with get_db() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_stock_dataframe(
    user: Optional[Dict[str, Any]] = None,
    depot_id: Optional[int] = None,
    product_id: Optional[int] = None,
) -> pd.DataFrame:
    """Return stock lines (depot x product), scoped to the user."""
    query = """
        SELECT
            s.id,
            d.id AS depot_id,
            d.code AS depot_code,
            d.name AS depot_name,
            c.name AS cic_name,
            c.wilaya,
            p.id AS product_id,
            p.code AS product_code,
            p.name AS product_name,
            p.category,
            p.unit,
            p.minimum_stock,
            s.quantity,
            CASE
                WHEN s.quantity <= p.minimum_stock THEN 'BAS'
                ELSE 'OK'
            END AS stock_status,
            s.updated_at
        FROM stocks s
        INNER JOIN depots d ON d.id = s.depot_id
        INNER JOIN cics c ON c.id = d.cic_id
        INNER JOIN products p ON p.id = s.product_id
        WHERE d.active = 1
          AND p.active = 1
    """
    params: List[Any] = []

    if user is not None:
        ids = get_scope_depot_ids(user)
        if ids is not None:
            clause, p = in_clause(ids)
            query += f" AND d.id IN {clause}"
            params.extend(p)

    if depot_id is not None:
        query += " AND d.id = ?"
        params.append(depot_id)

    if product_id is not None:
        query += " AND p.id = ?"
        params.append(product_id)

    query += " ORDER BY c.name, d.name, p.name"

    with get_db() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_stock_quantity(depot_id: int, product_id: int) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT quantity FROM stocks WHERE depot_id = ? AND product_id = ?",
            (depot_id, product_id),
        ).fetchone()
    return float(row["quantity"]) if row else 0.0


def set_stock(
    user: Dict[str, Any],
    depot_id: int,
    product_id: int,
    quantity: float,
) -> float:
    """Manual stock update (Admin: any depot / Agent: his own depot only)."""
    if not can_adjust_stock(user):
        raise PermissionError("Vous n'avez pas le droit de modifier le stock.")

    if quantity < 0:
        raise ValueError("La quantité de stock ne peut pas être négative.")

    if user["role"] == ROLE_AGENT and depot_id != user.get("depot_id"):
        raise PermissionError("Vous ne pouvez modifier que le stock de votre dépôt.")

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, quantity FROM stocks WHERE depot_id = ? AND product_id = ?",
            (depot_id, product_id),
        ).fetchone()

        if row is None and user["role"] != ROLE_ADMIN:
            raise PermissionError("Ce produit n'existe pas dans votre dépôt.")

        old_quantity = float(row["quantity"]) if row else 0.0

        if row:
            conn.execute(
                """
                UPDATE stocks
                SET quantity = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (quantity, row["id"]),
            )
            stock_id = row["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO stocks (depot_id, product_id, quantity) VALUES (?, ?, ?)",
                (depot_id, product_id, quantity),
            )
            stock_id = cursor.lastrowid

        depot = conn.execute(
            "SELECT code FROM depots WHERE id = ?", (depot_id,)
        ).fetchone()
        product = conn.execute(
            "SELECT code FROM products WHERE id = ?", (product_id,)
        ).fetchone()

        _audit(
            conn,
            "STOCK",
            stock_id,
            "MANUAL_ADJUSTMENT",
            str(old_quantity),
            f"{quantity} ({depot['code']} / {product['code']})",
            user,
        )

    return old_quantity


def _change_stock(
    conn: sqlite3.Connection,
    depot_id: int,
    product_id: int,
    delta: float,
):
    """Add / remove stock inside an existing transaction."""
    conn.execute(
        "INSERT OR IGNORE INTO stocks (depot_id, product_id, quantity) VALUES (?, ?, 0)",
        (depot_id, product_id),
    )
    row = conn.execute(
        "SELECT quantity FROM stocks WHERE depot_id = ? AND product_id = ?",
        (depot_id, product_id),
    ).fetchone()

    new_quantity = float(row["quantity"]) + delta

    if new_quantity < -1e-9:
        raise ValueError(
            "Stock insuffisant dans le dépôt source. "
            f"Stock disponible : {format_number(float(row['quantity']))}."
        )

    conn.execute(
        """
        UPDATE stocks
        SET quantity = ?, updated_at = CURRENT_TIMESTAMP
        WHERE depot_id = ? AND product_id = ?
        """,
        (max(new_quantity, 0.0), depot_id, product_id),
    )


STOCK_IMPORT_ALIASES = {
    "depot_code": {"depot_code", "code_depot", "code depot", "code dépôt", "depot", "dépôt"},
    "product_code": {"product_code", "code_produit", "code produit", "produit", "code"},
    "quantity": {"quantity", "quantite", "quantité", "qte", "qté", "stock"},
}


def build_stock_template(user: Dict[str, Any]) -> bytes:
    """Build the Excel template (data sheet + reference codes sheet)."""
    depots = get_depots(user)
    products = get_products(user if not is_admin(user) else None)

    sample_depot = depots.iloc[0]["code"] if not depots.empty else "DEP-XXX-01"
    sample_product = products.iloc[0]["code"] if not products.empty else "BT-001"

    template = pd.DataFrame(
        [{"depot_code": sample_depot, "product_code": sample_product, "quantity": 100}]
    )
    ref_depots = depots[["code", "name", "cic_name"]].rename(
        columns={"code": "depot_code", "name": "Dépôt", "cic_name": "CIC"}
    )
    ref_products = products[["code", "name", "unit"]].rename(
        columns={"code": "product_code", "name": "Produit", "unit": "Unité"}
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        template.to_excel(writer, sheet_name="Stock", index=False)
        ref_depots.to_excel(writer, sheet_name="Codes dépôts", index=False)
        ref_products.to_excel(writer, sheet_name="Codes produits", index=False)
    return buffer.getvalue()


def parse_stock_import(
    df: pd.DataFrame,
    user: Dict[str, Any],
    default_depot_id: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Validate an uploaded stock sheet. Returns (valid_rows, errors)."""
    mapping = {}
    for col in df.columns:
        key = str(col).strip().lower()
        for target, aliases in STOCK_IMPORT_ALIASES.items():
            if key in aliases and target not in mapping.values():
                mapping[col] = target
                break
    df = df.rename(columns=mapping)

    missing = [c for c in ("product_code", "quantity") if c not in df.columns]
    if missing:
        raise ValueError(
            f"Colonnes manquantes : {', '.join(missing)}. "
            "Colonnes attendues : depot_code, product_code, quantity."
        )

    has_depot_col = "depot_code" in df.columns

    if user["role"] == ROLE_ADMIN and not has_depot_col and default_depot_id is None:
        raise ValueError(
            "Ajoutez une colonne depot_code au fichier ou choisissez un dépôt par défaut."
        )

    with get_db() as conn:
        depots = {
            r["code"].upper(): int(r["id"])
            for r in conn.execute("SELECT id, code FROM depots WHERE active = 1")
        }
        if user["role"] == ROLE_ADMIN:
            products = {
                r["code"].upper(): int(r["id"])
                for r in conn.execute("SELECT id, code FROM products WHERE active = 1")
            }
        else:
            products = {
                r["code"].upper(): int(r["id"])
                for r in conn.execute(
                    """
                    SELECT p.id, p.code FROM products p
                    INNER JOIN stocks s ON s.product_id = p.id
                    WHERE p.active = 1 AND s.depot_id = ?
                    """,
                    (user.get("depot_id"),),
                )
            }

    code_by_id = {v: k for k, v in depots.items()}

    if user["role"] == ROLE_AGENT and user.get("depot_id") not in code_by_id:
        raise ValueError("Aucun dépôt actif ne vous est rattaché.")

    valid: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    seen = set()

    for idx, row in df.iterrows():
        line = int(idx) + 2  # ligne Excel (en-tête = ligne 1)

        raw_p = row["product_code"]
        p_code = "" if pd.isna(raw_p) else str(raw_p).strip().upper()
        raw_q = row["quantity"]

        if not p_code and pd.isna(raw_q):
            continue  # ligne vide

        def fail(msg: str):
            errors.append({"Ligne": line, "Produit": p_code or "—", "Erreur": msg})

        # Dépôt
        file_depot = ""
        if has_depot_col and not pd.isna(row["depot_code"]):
            file_depot = str(row["depot_code"]).strip().upper()

        if user["role"] == ROLE_AGENT:
            depot_id = user["depot_id"]
            if file_depot and file_depot != code_by_id[depot_id]:
                fail(f"Dépôt {file_depot} interdit : vous ne gérez que {code_by_id[depot_id]}.")
                continue
        elif file_depot:
            depot_id = depots.get(file_depot)
            if depot_id is None:
                fail(f"Dépôt inconnu : {file_depot}.")
                continue
        else:
            depot_id = default_depot_id
            if depot_id is None:
                fail("Dépôt manquant.")
                continue

        # Produit
        if not p_code:
            fail("Code produit manquant.")
            continue
        product_id = products.get(p_code)
        if product_id is None:
            fail(
                "Produit inconnu."
                if user["role"] == ROLE_ADMIN
                else "Produit absent de votre dépôt."
            )
            continue

        # Quantité
        if isinstance(raw_q, str):
            raw_q = raw_q.replace(" ", "").replace(",", ".")
        quantity = pd.to_numeric(raw_q, errors="coerce")
        if pd.isna(quantity) or quantity < 0:
            fail("Quantité invalide (nombre ≥ 0 attendu).")
            continue

        key = (depot_id, product_id)
        if key in seen:
            fail("Doublon (même dépôt et même produit déjà présent dans le fichier).")
            continue
        seen.add(key)

        valid.append(
            {
                "depot_id": depot_id,
                "depot_code": code_by_id[depot_id],
                "product_id": product_id,
                "product_code": p_code,
                "quantity": float(quantity),
            }
        )

    return valid, errors


def apply_stock_import(
    user: Dict[str, Any],
    rows: List[Dict[str, Any]],
    mode: str,
) -> int:
    """Apply validated rows in ONE transaction. mode: 'replace' or 'add'."""
    if not can_adjust_stock(user):
        raise PermissionError("Vous n'avez pas le droit de modifier le stock.")

    if user["role"] == ROLE_AGENT and any(
        r["depot_id"] != user.get("depot_id") for r in rows
    ):
        raise PermissionError("Import limité à votre dépôt.")

    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for r in rows:
            current = conn.execute(
                "SELECT quantity FROM stocks WHERE depot_id = ? AND product_id = ?",
                (r["depot_id"], r["product_id"]),
            ).fetchone()
            old = float(current["quantity"]) if current else 0.0
            new = r["quantity"] if mode == "replace" else old + r["quantity"]

            if current:
                conn.execute(
                    """
                    UPDATE stocks SET quantity = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE depot_id = ? AND product_id = ?
                    """,
                    (new, r["depot_id"], r["product_id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO stocks (depot_id, product_id, quantity) VALUES (?, ?, ?)",
                    (r["depot_id"], r["product_id"], new),
                )

            _audit(
                conn, "STOCK", None, "IMPORT_EXCEL", str(old),
                f"{new} ({r['depot_code']} / {r['product_code']})", user,
            )
    return len(rows)


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
            t.source_depot_id,
            sd.name AS source_depot,
            sc.name AS source_cic,
            t.destination_depot_id,
            dd.name AS destination_depot,
            dc.name AS destination_cic,
            p.code AS product_code,
            p.name AS product,
            p.unit,
            t.quantity,
            t.transfer_date,
            t.expected_date,
            t.status,
            t.reason,
            t.truck_plate,
            t.driver_name,
            u.full_name AS created_by_name,
            t.shipped_at,
            t.received_at,
            t.completed_at,
            t.created_at,
            t.updated_at
        FROM transfers t
        INNER JOIN depots sd ON sd.id = t.source_depot_id
        INNER JOIN cics sc ON sc.id = sd.cic_id
        INNER JOIN depots dd ON dd.id = t.destination_depot_id
        INNER JOIN cics dc ON dc.id = dd.cic_id
        INNER JOIN products p ON p.id = t.product_id
        INNER JOIN users u ON u.id = t.created_by
        WHERE 1 = 1
    """
    params: List[Any] = []

    if user is not None:
        ids = get_scope_depot_ids(user)
        if ids is not None:
            clause, p = in_clause(ids)
            query += (
                f" AND (t.source_depot_id IN {clause} "
                f"OR t.destination_depot_id IN {clause})"
            )
            params.extend(p)
            params.extend(p)

    if status and status != "Tous":
        query += " AND t.status = ?"
        params.append(status)

    query += " ORDER BY t.id DESC"

    with get_db() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_transfer(transfer_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM transfers WHERE id = ?", (transfer_id,)
        ).fetchone()
    return dict(row) if row else None


def _generate_reference(conn: sqlite3.Connection) -> str:
    for _ in range(20):
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        reference = f"AGD-{stamp}-{secrets.token_hex(2).upper()}"
        exists = conn.execute(
            "SELECT id FROM transfers WHERE reference = ?", (reference,)
        ).fetchone()
        if not exists:
            return reference
    raise RuntimeError("Impossible de générer une référence unique.")


# =============================================================================
# TRANSFER WORKFLOW
# =============================================================================

def create_transfer(
    source_depot_id: int,
    destination_depot_id: int,
    product_id: int,
    quantity: float,
    transfer_date: str,
    expected_date: Optional[str],
    reason: str,
    truck_plate: str,
    driver_name: str,
    user: Dict[str, Any],
) -> int:
    """Create a depot-to-depot transfer request in Pending state."""
    if not can_create_transfer(user):
        raise PermissionError("Votre rôle ne permet pas de créer un flux.")

    if user["role"] == ROLE_AGENT and source_depot_id != user.get("depot_id"):
        raise PermissionError(
            "Vous ne pouvez créer un flux qu'à partir de votre dépôt."
        )

    if quantity <= 0:
        raise ValueError("La quantité doit être supérieure à zéro.")

    if source_depot_id == destination_depot_id:
        raise ValueError("Le dépôt source et le dépôt destination doivent être différents.")

    truck_plate = (truck_plate or "").strip()
    driver_name = (driver_name or "").strip()

    if not truck_plate or not driver_name:
        raise ValueError(
            "Le matricule du camion et le nom du chauffeur sont obligatoires."
        )

    if expected_date and expected_date < transfer_date:
        raise ValueError("La date prévue ne peut pas précéder la date du flux.")

    with get_db() as conn:
        depots = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, cic_id, active FROM depots WHERE id IN (?, ?)",
                (source_depot_id, destination_depot_id),
            ).fetchall()
        }

        if len(depots) != 2 or not all(d["active"] for d in depots.values()):
            raise ValueError("Dépôt source ou destination invalide.")

        transfer_type = (
            "Intra-CIC"
            if depots[source_depot_id]["cic_id"]
            == depots[destination_depot_id]["cic_id"]
            else "Inter-CIC"
        )

        product = conn.execute(
            "SELECT active FROM products WHERE id = ?", (product_id,)
        ).fetchone()

        if not product or not product["active"]:
            raise ValueError("Produit invalide.")

        stock = conn.execute(
            "SELECT quantity FROM stocks WHERE depot_id = ? AND product_id = ?",
            (source_depot_id, product_id),
        ).fetchone()
        available = float(stock["quantity"]) if stock else 0.0

        if quantity > available:
            raise ValueError(
                f"Stock insuffisant. Stock disponible : {format_number(available)}."
            )

        reference = _generate_reference(conn)

        cursor = conn.execute(
            """
            INSERT INTO transfers
            (
                reference, transfer_type, source_depot_id, destination_depot_id,
                product_id, quantity, transfer_date, expected_date, status,
                reason, truck_plate, driver_name, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference, transfer_type, source_depot_id, destination_depot_id,
                product_id, quantity, transfer_date, expected_date,
                STATUS_PENDING, (reason or "").strip(), truck_plate,
                driver_name, user["id"],
            ),
        )
        transfer_id = cursor.lastrowid

        _audit(conn, "TRANSFER", transfer_id, "CREATE", None,
               STATUS_PENDING, user)

    return transfer_id


def update_transfer_status(
    transfer_id: int,
    new_status: str,
    user: Dict[str, Any],
):
    """
    Apply a controlled status transition (single atomic transaction).

    Pending    -> In Transit : sortie validée par le chef du dépôt source,
                               stock source déduit.
    In Transit -> Received   : réception validée par le chef du dépôt destinataire,
                               stock destination augmenté.
    Received   -> Completed  : clôture par le chef du dépôt destinataire.
    Pending    -> Cancelled  : aucun impact sur le stock.
    In Transit -> Cancelled  : Admin uniquement, stock source restitué.
    """
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT * FROM transfers WHERE id = ?", (transfer_id,)
        ).fetchone()

        if not row:
            raise ValueError("Flux introuvable.")

        transfer = dict(row)
        old_status = transfer["status"]

        if new_status not in TRANSITIONS.get(old_status, []):
            raise ValueError(f"Transition interdite : {old_status} → {new_status}.")

        if new_status not in available_actions(user, transfer):
            raise PermissionError(
                "Vous n'avez pas le droit d'effectuer cette action sur ce flux."
            )

        source = transfer["source_depot_id"]
        destination = transfer["destination_depot_id"]
        product_id = transfer["product_id"]
        quantity = float(transfer["quantity"])

        if new_status == STATUS_IN_TRANSIT:
            if not transfer["truck_plate"] or not transfer["driver_name"]:
                raise ValueError("Matricule camion et chauffeur obligatoires.")
            _change_stock(conn, source, product_id, -quantity)

        elif old_status == STATUS_IN_TRANSIT and new_status == STATUS_CANCELLED:
            _change_stock(conn, source, product_id, +quantity)

        elif new_status == STATUS_RECEIVED:
            _change_stock(conn, destination, product_id, +quantity)

        extra_columns = {
            STATUS_IN_TRANSIT: ("shipped_by", "shipped_at"),
            STATUS_RECEIVED: ("received_by", "received_at"),
            STATUS_COMPLETED: ("completed_by", "completed_at"),
        }

        set_clause = "status = ?, updated_at = CURRENT_TIMESTAMP"
        params: List[Any] = [new_status]

        if new_status in extra_columns:
            by_col, at_col = extra_columns[new_status]
            set_clause += f", {by_col} = ?, {at_col} = CURRENT_TIMESTAMP"
            params.append(user["id"])

        params.append(transfer_id)

        conn.execute(
            f"UPDATE transfers SET {set_clause} WHERE id = ?", params
        )

        _audit(conn, "TRANSFER", transfer_id, "STATUS_CHANGE",
               old_status, new_status, user)


# =============================================================================
# DELETION (with integrity checks)
# =============================================================================

def set_user_password(
    target_id: int,
    new_password: str,
    actor: Dict[str, Any],
    old_password: Optional[str] = None,
) -> str:
    """
    Change a password.
    - Own account : the current password is required.
    - Other account : Admin only (reset, no current password needed).
    """
    if len(new_password) < 6:
        raise ValueError("Le mot de passe doit contenir au moins 6 caractères.")

    own = target_id == actor["id"]

    if not own and not is_admin(actor):
        raise PermissionError("Seul l'Admin peut changer le mot de passe d'un autre utilisateur.")

    with get_db() as conn:
        target = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE id = ?", (target_id,)
        ).fetchone()
        if not target:
            raise ValueError("Utilisateur introuvable.")

        if own and not verify_password(old_password or "", target["password_hash"]):
            raise ValueError("Mot de passe actuel incorrect.")

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), target_id),
        )
        _audit(
            conn, "USER", target_id,
            "PASSWORD_CHANGE" if own else "PASSWORD_RESET",
            None, target["username"], actor,
        )

    return f"Mot de passe de « {target['username']} » modifié."


def delete_product(product_id: int, user: Dict[str, Any]) -> str:
    if not is_admin(user):
        raise PermissionError("Seul l'Admin peut supprimer un produit.")

    with get_db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if not product:
            raise ValueError("Produit introuvable.")

        total = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS q FROM stocks WHERE product_id = ?",
            (product_id,),
        ).fetchone()["q"]

        if total > 0:
            raise ValueError(
                "Suppression impossible : ce produit a encore du stock "
                f"({format_number(total)}). Videz d'abord le stock des dépôts."
            )

        used = conn.execute(
            "SELECT COUNT(*) AS c FROM transfers WHERE product_id = ?",
            (product_id,),
        ).fetchone()["c"]

        conn.execute("DELETE FROM stocks WHERE product_id = ?", (product_id,))

        if used:
            conn.execute("UPDATE products SET active = 0 WHERE id = ?", (product_id,))
            message = (
                f"Le produit « {product['name']} » est lié à l'historique des flux : "
                "il a été archivé (désactivé) au lieu d'être supprimé."
            )
            action = "ARCHIVE"
        else:
            conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
            message = f"Produit « {product['name']} » supprimé."
            action = "DELETE"

        _audit(conn, "PRODUCT", product_id, action, product["name"], None, user)

    return message


def delete_depot(depot_id: int, user: Dict[str, Any]) -> str:
    if not is_admin(user):
        raise PermissionError("Seul l'Admin peut supprimer un dépôt.")

    with get_db() as conn:
        depot = conn.execute(
            "SELECT * FROM depots WHERE id = ?", (depot_id,)
        ).fetchone()
        if not depot:
            raise ValueError("Dépôt introuvable.")

        total = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS q FROM stocks WHERE depot_id = ?",
            (depot_id,),
        ).fetchone()["q"]
        if total > 0:
            raise ValueError(
                "Suppression impossible : le dépôt contient encore du stock. "
                "Transférez ou videz le stock d'abord."
            )

        agents = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE depot_id = ?", (depot_id,)
        ).fetchone()["c"]
        if agents:
            raise ValueError(
                "Suppression impossible : des utilisateurs sont rattachés à ce dépôt. "
                "Supprimez-les ou réaffectez-les d'abord."
            )

        open_transfers = conn.execute(
            """
            SELECT COUNT(*) AS c FROM transfers
            WHERE (source_depot_id = ? OR destination_depot_id = ?)
              AND status IN ('Pending', 'In Transit', 'Received')
            """,
            (depot_id, depot_id),
        ).fetchone()["c"]
        if open_transfers:
            raise ValueError(
                "Suppression impossible : des flux sont encore en cours pour ce dépôt."
            )

        used = conn.execute(
            """
            SELECT COUNT(*) AS c FROM transfers
            WHERE source_depot_id = ? OR destination_depot_id = ?
            """,
            (depot_id, depot_id),
        ).fetchone()["c"]

        conn.execute("DELETE FROM stocks WHERE depot_id = ?", (depot_id,))

        if used:
            conn.execute("UPDATE depots SET active = 0 WHERE id = ?", (depot_id,))
            message = (
                f"Le dépôt « {depot['name']} » possède un historique de flux : "
                "il a été archivé (désactivé) au lieu d'être supprimé."
            )
            action = "ARCHIVE"
        else:
            conn.execute("DELETE FROM depots WHERE id = ?", (depot_id,))
            message = f"Dépôt « {depot['name']} » supprimé."
            action = "DELETE"

        _audit(conn, "DEPOT", depot_id, action, depot["name"], None, user)

    return message


def delete_cic(cic_id: int, user: Dict[str, Any]) -> str:
    if not is_admin(user):
        raise PermissionError("Seul l'Admin peut supprimer un CIC.")

    with get_db() as conn:
        cic = conn.execute("SELECT * FROM cics WHERE id = ?", (cic_id,)).fetchone()
        if not cic:
            raise ValueError("CIC introuvable.")

        depots = conn.execute(
            "SELECT COUNT(*) AS c FROM depots WHERE cic_id = ?", (cic_id,)
        ).fetchone()["c"]
        if depots:
            raise ValueError(
                "Suppression impossible : ce CIC contient encore des dépôts. "
                "Supprimez d'abord ses dépôts."
            )

        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE cic_id = ?", (cic_id,)
        ).fetchone()["c"]
        if users:
            raise ValueError(
                "Suppression impossible : des utilisateurs sont rattachés à ce CIC."
            )

        conn.execute("DELETE FROM cics WHERE id = ?", (cic_id,))
        _audit(conn, "CIC", cic_id, "DELETE", cic["name"], None, user)

    return f"CIC « {cic['name']} » supprimé."


def delete_user(target_id: int, user: Dict[str, Any]) -> str:
    if not is_admin(user):
        raise PermissionError("Seul l'Admin peut supprimer un utilisateur.")

    if target_id == user["id"]:
        raise ValueError("Vous ne pouvez pas supprimer votre propre compte.")

    with get_db() as conn:
        target = conn.execute(
            "SELECT * FROM users WHERE id = ?", (target_id,)
        ).fetchone()
        if not target:
            raise ValueError("Utilisateur introuvable.")

        if target["role"] == ROLE_ADMIN:
            others = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = 'Admin' AND active = 1 AND id <> ?",
                (target_id,),
            ).fetchone()["c"]
            if others == 0:
                raise ValueError("Impossible de supprimer le dernier administrateur.")

        used = conn.execute(
            """
            SELECT COUNT(*) AS c FROM transfers
            WHERE created_by = ? OR shipped_by = ?
               OR received_by = ? OR completed_by = ?
            """,
            (target_id, target_id, target_id, target_id),
        ).fetchone()["c"]

        if used:
            conn.execute("UPDATE users SET active = 0 WHERE id = ?", (target_id,))
            message = (
                f"L'utilisateur « {target['username']} » a un historique de flux : "
                "son compte a été désactivé au lieu d'être supprimé."
            )
            action = "DEACTIVATE"
        else:
            # Le nom d'utilisateur reste conservé dans le journal d'audit.
            conn.execute(
                "UPDATE audit_logs SET user_id = NULL WHERE user_id = ?",
                (target_id,),
            )
            conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
            message = f"Utilisateur « {target['username']} » supprimé."
            action = "DELETE"

        _audit(conn, "USER", target_id, action, target["username"], None, user)

    return message


# =============================================================================
# UI HELPERS
# =============================================================================

def apply_css():
    st.markdown(
        f"""
        <style>
        .stApp {{ background: #F8FAFC; }}

        [data-testid="stSidebar"] {{
            background: linear-gradient(180deg, {AGRODIV_NAVY} 0%, #10251A 100%);
        }}
        [data-testid="stSidebar"] * {{ color: #FFFFFF !important; }}

        .brand-header {{
            background: linear-gradient(135deg, {AGRODIV_NAVY}, {AGRODIV_DARK_GREEN});
            padding: 24px 30px;
            border-radius: 16px;
            color: white;
            margin-bottom: 24px;
            box-shadow: 0 8px 25px rgba(15, 23, 42, 0.12);
        }}
        .brand-header h1 {{ margin: 0; font-size: 27px; font-weight: 800; color: white; }}
        .brand-header p {{ margin: 7px 0 0 0; opacity: 0.88; }}

        .metric-card {{
            background: white;
            border: 1px solid {AGRODIV_BORDER};
            border-radius: 14px;
            padding: 18px;
            box-shadow: 0 3px 12px rgba(15, 23, 42, 0.05);
        }}
        .metric-title {{ color: #64748B; font-size: 13px; font-weight: 600; }}
        .metric-value {{
            color: {AGRODIV_NAVY}; font-size: 26px; font-weight: 800; margin-top: 5px;
        }}

        .login-container {{ max-width: 440px; margin: 70px auto; }}

        .footer {{
            text-align: center; color: #94A3B8; font-size: 12px; padding: 30px 0 10px;
        }}

        div[data-testid="stMetric"] {{
            background: white;
            border: 1px solid {AGRODIV_BORDER};
            padding: 12px;
            border-radius: 12px;
        }}
        .stButton > button {{ border-radius: 8px; font-weight: 600; }}

        .brand-flex {{
            display: flex; align-items: center;
            justify-content: space-between; gap: 20px;
        }}
        .brand-logo {{
            height: 68px; background: white; border-radius: 14px;
            padding: 6px 10px; box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
        }}
        .logo-card {{
            background: white; border-radius: 22px; padding: 22px 18px;
            text-align: center; margin-bottom: 22px;
            border: 1px solid {AGRODIV_BORDER};
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.10);
        }}
        .logo-card img {{ width: 78%; max-width: 280px; height: auto; }}
        .logo-card-sidebar {{
            background: white; border-radius: 16px; padding: 12px;
            text-align: center; margin: 4px 0 14px;
            box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25);
        }}
        .logo-card-sidebar img {{ width: 85%; height: auto; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


@lru_cache(maxsize=1)
def get_logo_b64() -> Optional[str]:
    """Return the logo as base64 (None if logo.png is missing)."""
    try:
        with open(LOGO_PATH, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


def logo_img_tag(css_class: str = "") -> str:
    data = get_logo_b64()
    if not data:
        return ""
    return f'<img class="{css_class}" src="data:image/png;base64,{data}" alt="AGRODIV">'


def show_header(title: str, subtitle: str = ""):
    subtitle_html = f"<p>{subtitle}</p>" if subtitle else ""
    st.markdown(
        f"""
        <div class="brand-header brand-flex">
            <div>
                <h1>{title}</h1>
                {subtitle_html}
            </div>
            {logo_img_tag("brand-logo")}
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_card(title: str, value: str, icon: str = ""):
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
    return f"{value:,.2f}".replace(",", " ")


def status_badge(status: str) -> str:
    colors = {
        STATUS_PENDING: ("#FEF3C7", "#92400E"),
        STATUS_IN_TRANSIT: ("#E0E7FF", "#4338CA"),
        STATUS_RECEIVED: ("#DCFCE7", "#166534"),
        STATUS_COMPLETED: ("#D1FAE5", "#065F46"),
        STATUS_CANCELLED: ("#F1F5F9", "#475569"),
    }
    bg, fg = colors.get(status, ("#F1F5F9", "#475569"))
    return (
        f'<span style="background:{bg};color:{fg};'
        f'padding:4px 10px;border-radius:999px;font-weight:700;'
        f'font-size:12px;">{status}</span>'
    )


def flash_success(message: str):
    st.session_state["flash_success"] = message


def flash_error(message: str):
    st.session_state["flash_error"] = message


def show_flash_messages():
    if "flash_success" in st.session_state:
        st.success(st.session_state.pop("flash_success"))
    if "flash_error" in st.session_state:
        st.error(st.session_state.pop("flash_error"))


def dataframe_download_button(dataframe: pd.DataFrame, filename: str, label: str):
    csv_data = dataframe.to_csv(index=False).encode("utf-8-sig")
    st.download_button(label=label, data=csv_data, file_name=filename, mime="text/csv")


def render_low_stock_alerts(stock: pd.DataFrame):
    """Display the low-stock alert block."""
    if stock.empty:
        return

    low = stock[stock["stock_status"] == "BAS"]
    if low.empty:
        return

    st.error(
        f"🚨 {len(low)} alerte(s) de stock bas — "
        "quantité inférieure ou égale au seuil défini par l'Admin."
    )
    st.dataframe(
        low[
            [
                "depot_name", "cic_name", "product_name",
                "quantity", "minimum_stock", "unit",
            ]
        ].rename(
            columns={
                "depot_name": "Dépôt",
                "cic_name": "CIC",
                "product_name": "Produit",
                "quantity": "Stock actuel",
                "minimum_stock": "Seuil",
                "unit": "Unité",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )


def delete_box(
    title: str,
    options: Dict[str, int],
    key: str,
    delete_fn,
    user: Dict[str, Any],
    warning: Optional[str] = None,
):
    """Generic 'select + confirm + delete' block."""
    with st.expander(title):
        if not options:
            st.info("Aucun élément à supprimer.")
            return

        choice = st.selectbox(
            "Élément à supprimer", list(options.keys()), key=f"{key}_sel"
        )
        item_id = options[choice]

        if warning:
            st.warning(warning)

        confirm = st.checkbox(
            "Je confirme la suppression",
            key=f"{key}_confirm_{item_id}",
        )

        clicked = st.button(
            "🗑️ Supprimer",
            key=f"{key}_btn",
            type="primary",
            disabled=not confirm,
        )

        if clicked:
            try:
                message = delete_fn(item_id, user)
            except (ValueError, PermissionError) as exc:
                st.error(str(exc))
                return

            flash_success(message)
            st.rerun()


# =============================================================================
# LOGIN
# =============================================================================

def login_page():
    st.markdown('<div class="login-container">', unsafe_allow_html=True)

    if get_logo_b64():
        st.markdown(
            f'<div class="logo-card">{logo_img_tag()}</div>',
            unsafe_allow_html=True,
        )

    st.markdown(
        """
        <div class="brand-header">
            <h1>AGRODIV</h1>
            <p>Filiale Céréales Ouest</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("Connexion")
    st.caption("Gestion sécurisée des flux commerciaux entre dépôts et CIC")

    with st.form("login_form"):
        username = st.text_input("Nom d'utilisateur", placeholder="Nom d'utilisateur")
        password = st.text_input(
            "Mot de passe", type="password", placeholder="Votre mot de passe"
        )
        submitted = st.form_submit_button(
            "Se connecter", type="primary", use_container_width=True
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
                    "AUTH", user["id"], "LOGIN", new_value="SUCCESS",
                    user_id=user["id"], username=user["username"],
                )
                st.rerun()
            else:
                st.error("Identifiants invalides.")

    st.info("MADE BY AZZOUZ MOHAMMED IHAB")
    st.markdown("</div>", unsafe_allow_html=True)


# =============================================================================
# SIDEBAR
# =============================================================================

def render_sidebar(user: Dict[str, Any]) -> str:
    with st.sidebar:
        if get_logo_b64():
            st.markdown(
                f'<div class="logo-card-sidebar">{logo_img_tag()}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                """
                <div style="padding:8px 4px 20px;">
                    <div style="font-size:25px;font-weight:900;">AGRODIV</div>
                    <div style="font-size:12px;opacity:.75;">Filiale Céréales Ouest</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        st.markdown("---")

        st.markdown(f"**Utilisateur**  \n{user['full_name']}")
        st.caption(f"Rôle : {user['role']}")

        with get_db() as conn:
            if user.get("depot_id"):
                depot = conn.execute(
                    "SELECT name FROM depots WHERE id = ?", (user["depot_id"],)
                ).fetchone()
                if depot:
                    st.caption(f"Chef du dépôt : {depot['name']}")
            if user.get("cic_id"):
                cic = conn.execute(
                    "SELECT name FROM cics WHERE id = ?", (user["cic_id"],)
                ).fetchone()
                if cic:
                    st.caption(f"CIC : {cic['name']}")

        st.markdown("---")

        pages = [PAGE_DASH, PAGE_FLUX, PAGE_STOCK]
        if user["role"] in (ROLE_ADMIN, ROLE_MANAGER):
            pages.append(PAGE_CIC)
        pages.append(PAGE_AUDIT)
        pages.append(PAGE_PROFILE)
        if is_admin(user):
            pages.append(PAGE_USERS)

        selected = st.radio("Navigation", pages, label_visibility="collapsed")

        st.markdown("---")

        if st.button("🚪 Déconnexion", use_container_width=True):
            write_audit(
                "AUTH", user["id"], "LOGOUT", new_value="SUCCESS",
                user_id=user["id"], username=user["username"],
            )
            st.session_state.clear()
            st.rerun()

        st.markdown(
            '<div class="footer">MADE BY AZZOUZ MOHAMMED IHAB</div>',
            unsafe_allow_html=True,
        )

    return selected


# =============================================================================
# DASHBOARD
# =============================================================================

def dashboard_page(user: Dict[str, Any]):
    show_header(
        "Tableau de bord",
        "Vue consolidée des stocks et flux entre dépôts AGRODIV",
    )

    transfers = get_transfers(user)
    stock = get_stock_dataframe(user)

    total_stock = float(stock["quantity"].sum()) if not stock.empty else 0.0
    low_stock = int((stock["stock_status"] == "BAS").sum()) if not stock.empty else 0

    pending = int((transfers["status"] == STATUS_PENDING).sum()) if not transfers.empty else 0
    in_transit = int((transfers["status"] == STATUS_IN_TRANSIT).sum()) if not transfers.empty else 0
    done = (
        int(transfers["status"].isin([STATUS_COMPLETED, STATUS_RECEIVED]).sum())
        if not transfers.empty else 0
    )

    # Rappel pour le chef de dépôt : flux qui attendent son action
    if user["role"] == ROLE_AGENT and not transfers.empty:
        todo = sum(
            1 for r in transfers.to_dict("records") if available_actions(user, r)
        )
        if todo:
            st.info(f"🔔 {todo} flux nécessitent une action de votre part (onglet Flux).")

    if user["role"] == ROLE_MANAGER:
        st.caption("Mode consultation : vous visualisez les dépôts de votre CIC.")

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        metric_card("Stock total", f"{format_number(total_stock)} QX", "📦")
    with c2:
        metric_card("En attente", str(pending), "⏳")
    with c3:
        metric_card("En transit", str(in_transit), "🚚")
    with c4:
        metric_card("Flux réalisés", str(done), "✅")
    with c5:
        metric_card("Alertes stock bas", str(low_stock), "⚠️")

    st.markdown("###")

    render_low_stock_alerts(stock)

    left, right = st.columns(2)

    with left:
        st.subheader("Stock par dépôt")
        if not stock.empty:
            chart = (
                stock.groupby("depot_name")["quantity"]
                .sum()
                .sort_values(ascending=False)
            )
            st.bar_chart(chart)
        else:
            st.info("Aucune donnée de stock.")

    with right:
        st.subheader("Répartition des flux")
        if not transfers.empty:
            counts = (
                transfers["status"].value_counts()
                .rename_axis("Statut").to_frame("Nombre")
            )
            st.bar_chart(counts)
        else:
            st.info("Aucun flux enregistré.")

    st.subheader("Statistiques par dépôt source")

    if not transfers.empty:
        stats = (
            transfers.groupby(["source_depot", "source_cic"])
            .agg(Nombre_Flux=("id", "count"), Quantite=("quantity", "sum"))
            .reset_index()
            .sort_values("Quantite", ascending=False)
        )
        stats["Quantite"] = stats["Quantite"].round(2)
        st.dataframe(stats, use_container_width=True, hide_index=True)

    st.subheader("Derniers mouvements")

    recent = transfers.head(10)
    if not recent.empty:
        st.dataframe(
            recent[
                [
                    "reference", "transfer_type", "source_depot",
                    "destination_depot", "product", "quantity",
                    "truck_plate", "driver_name", "status", "created_at",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Aucun mouvement récent.")


# =============================================================================
# TRANSFER PAGE
# =============================================================================

def transfer_creation_form(user: Dict[str, Any]):
    st.subheader("Créer une demande de flux (dépôt → dépôt)")

    if not can_create_transfer(user):
        st.info(
            "Mode consultation : seul le chef de dépôt (Agent Commercial) "
            "ou l'Admin peut créer un flux."
        )
        return

    all_depots = get_depots(None)

    if all_depots.empty:
        st.warning("Veuillez d'abord configurer les CIC et les dépôts.")
        return

    depot_labels = {
        int(r.id): f"{r.name} — {r.cic_name}" for r in all_depots.itertuples()
    }

    # Dépôt source
    if is_admin(user):
        source_label = st.selectbox(
            "Dépôt source", list(depot_labels.values()), key="flux_source_depot"
        )
        source_id = [k for k, v in depot_labels.items() if v == source_label][0]
    else:
        source_id = user.get("depot_id")
        if not source_id or source_id not in depot_labels:
            st.warning("Aucun dépôt actif ne vous est rattaché.")
            return
        st.info(f"Dépôt source : **{depot_labels[source_id]}**")

    source_stock = get_stock_dataframe(user, depot_id=source_id)
    source_stock = source_stock[source_stock["quantity"] > 0]

    if source_stock.empty:
        st.warning("Ce dépôt n'a aucun produit en stock.")
        return

    product_options = {
        f"{r.product_code} — {r.product_name} "
        f"(dispo : {format_number(r.quantity)} {r.unit})": int(r.product_id)
        for r in source_stock.itertuples()
    }

    destination_options = {
        label: depot_id
        for depot_id, label in depot_labels.items()
        if depot_id != source_id
    }

    if not destination_options:
        st.warning("Aucun autre dépôt disponible comme destination.")
        return

    with st.form("create_transfer"):
        destination_label = st.selectbox(
            "Dépôt destinataire", list(destination_options.keys())
        )
        product_label = st.selectbox("Produit", list(product_options.keys()))

        quantity = st.number_input(
            "Quantité", min_value=0.01, value=100.0, step=10.0
        )

        col1, col2 = st.columns(2)
        with col1:
            truck_plate = st.text_input(
                "Matricule du camion", placeholder="Ex. 12345-116-31"
            )
        with col2:
            driver_name = st.text_input(
                "Nom du chauffeur", placeholder="Nom et prénom"
            )

        col3, col4 = st.columns(2)
        with col3:
            transfer_date = st.date_input("Date du flux", value=date.today())
        with col4:
            expected_date = st.date_input(
                "Date prévue de réception", value=date.today()
            )

        reason = st.text_area(
            "Motif / observations",
            placeholder="Ex. Rééquilibrage de stock, demande commerciale...",
        )

        submitted = st.form_submit_button(
            "Créer la demande", type="primary", use_container_width=True
        )

    if submitted:
        try:
            transfer_id = create_transfer(
                source_depot_id=source_id,
                destination_depot_id=destination_options[destination_label],
                product_id=product_options[product_label],
                quantity=float(quantity),
                transfer_date=transfer_date.isoformat(),
                expected_date=expected_date.isoformat(),
                reason=reason,
                truck_plate=truck_plate,
                driver_name=driver_name,
                user=user,
            )
        except (ValueError, PermissionError) as exc:
            st.error(str(exc))
            return

        flash_success(
            f"Demande créée (ID {transfer_id}). Elle attend la validation "
            "de sortie du chef du dépôt source."
        )
        st.rerun()


ACTION_LABELS = {
    STATUS_IN_TRANSIT: "🚚 Valider la sortie du dépôt",
    STATUS_RECEIVED: "📥 Valider la réception (Received)",
    STATUS_COMPLETED: "🏁 Clôturer (Completed)",
    STATUS_CANCELLED: "❌ Annuler le flux",
}

ACTION_HELP = {
    STATUS_IN_TRANSIT: "Confirme que la marchandise a quitté votre dépôt : le stock source est déduit.",
    STATUS_RECEIVED: "Confirme la réception : le stock de votre dépôt est mis à jour automatiquement.",
    STATUS_COMPLETED: "Clôture définitivement le flux.",
    STATUS_CANCELLED: "Annule le flux (le stock source est restitué s'il était déjà sorti).",
}


def transfer_workflow_panel(user: Dict[str, Any]):
    st.subheader("Validation et suivi des flux")

    transfers = get_transfers(user)

    if transfers.empty:
        st.info("Aucun flux disponible.")
        return

    if user["role"] == ROLE_MANAGER:
        st.caption("Mode consultation : vous ne pouvez pas modifier les flux.")

    st.dataframe(
        transfers[
            [
                "id", "reference", "transfer_type", "source_depot",
                "destination_depot", "product", "quantity", "truck_plate",
                "driver_name", "transfer_date", "status",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("#### Détail et action sur un flux")

    options = {
        f"{r.reference} — {r.product} — {r.status}": int(r.id)
        for r in transfers.itertuples()
    }

    selected_label = st.selectbox("Sélectionner un flux", list(options.keys()))
    transfer_id = options[selected_label]

    transfer = get_transfer(transfer_id)
    row = transfers[transfers["id"] == transfer_id].iloc[0]

    if not transfer:
        st.error("Flux introuvable.")
        return

    st.markdown(
        f"""
**Référence :** `{row['reference']}`  
**Statut actuel :** {status_badge(row['status'])}  
**Trajet :** {row['source_depot']} ({row['source_cic']}) → {row['destination_depot']} ({row['destination_cic']})  
**Produit :** {row['product']} — {format_number(row['quantity'])} {row['unit']}  
**Camion :** {row['truck_plate']}  
**Chauffeur :** {row['driver_name']}  
**Créé par :** {row['created_by_name']}  
**Motif :** {row['reason'] or '—'}
        """,
        unsafe_allow_html=True,
    )

    actions = available_actions(user, transfer)

    if not actions:
        if user["role"] == ROLE_MANAGER:
            st.info("Consultation uniquement.")
        else:
            st.info("Aucune action disponible pour vous sur ce flux à ce stade.")
        return

    cols = st.columns(len(actions))
    clicked_action = None

    for col, action in zip(cols, actions):
        with col:
            if st.button(
                ACTION_LABELS[action],
                key=f"act_{transfer_id}_{action}",
                use_container_width=True,
                type="secondary" if action == STATUS_CANCELLED else "primary",
                help=ACTION_HELP[action],
            ):
                clicked_action = action

    if clicked_action:
        try:
            update_transfer_status(transfer_id, clicked_action, user)
        except (ValueError, PermissionError) as exc:
            st.error(str(exc))
            return

        flash_success(
            f"Le flux {transfer['reference']} est maintenant « {clicked_action} »."
        )
        st.rerun()


def transfer_reports(user: Dict[str, Any]):
    st.subheader("Rapports des flux")

    transfers = get_transfers(user)

    if transfers.empty:
        st.info("Aucun transfert à exporter.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        status_filter = st.selectbox("Filtrer par statut", ["Tous"] + ALL_STATUSES)
    with c2:
        type_filter = st.selectbox("Type", ["Tous", "Intra-CIC", "Inter-CIC"])
    with c3:
        product_filter = st.selectbox(
            "Produit", ["Tous"] + sorted(transfers["product"].unique().tolist())
        )

    filtered = transfers.copy()
    if status_filter != "Tous":
        filtered = filtered[filtered["status"] == status_filter]
    if type_filter != "Tous":
        filtered = filtered[filtered["transfer_type"] == type_filter]
    if product_filter != "Tous":
        filtered = filtered[filtered["product"] == product_filter]

    filtered = filtered.drop(columns=["source_depot_id", "destination_depot_id"])

    st.write(f"**{len(filtered)} flux trouvé(s)**")
    st.dataframe(filtered, use_container_width=True, hide_index=True)

    dataframe_download_button(
        filtered,
        f"agrodiv_flux_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        "⬇️ Exporter le rapport CSV",
    )


def transfers_page(user: Dict[str, Any]):
    show_header(
        "Flux inter-dépôts",
        "Création, validation de sortie / réception, suivi et reporting",
    )

    tab1, tab2, tab3 = st.tabs(
        ["➕ Nouvelle demande", "🔄 Workflow", "📊 Rapports"]
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
    st.subheader("Produits")

    admin = can_manage_products(user)
    products = get_products(user, active_only=not admin)

    if user["role"] != ROLE_ADMIN:
        st.caption("Vous voyez uniquement les produits présents dans vos dépôts.")

    if products.empty:
        st.info("Aucun produit.")
    else:
        st.dataframe(
            products.rename(columns={"minimum_stock": "seuil_stock_bas"}),
            use_container_width=True,
            hide_index=True,
        )

    if not admin:
        return

    st.caption(
        "Le « seuil de stock bas » est défini ici par l'Admin : une alerte "
        "apparaît quand le stock d'un dépôt est inférieur ou égal à ce seuil."
    )

    with st.expander("➕ Ajouter un produit"):
        with st.form("add_product"):
            code = st.text_input("Code produit")
            name = st.text_input("Désignation")
            category = st.selectbox("Catégorie", PRODUCT_CATEGORIES)
            unit = st.selectbox("Unité", PRODUCT_UNITS)
            minimum_stock = st.number_input(
                "Seuil de stock bas", min_value=0.0, value=50.0
            )
            submitted = st.form_submit_button("Ajouter", type="primary")

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
                            (code.strip().upper(), name.strip(), category,
                             unit, minimum_stock),
                        )
                        _audit(conn, "PRODUCT", cursor.lastrowid, "CREATE",
                               None, name.strip(), user)
                except sqlite3.IntegrityError:
                    st.error("Ce code produit existe déjà.")
                else:
                    flash_success("Produit ajouté.")
                    st.rerun()

    active_products = products[products["active"] == 1] if not products.empty else products

    with st.expander("✏️ Modifier un produit / seuil de stock bas"):
        if active_products.empty:
            st.info("Aucun produit.")
        else:
            product_map = {
                f"{r.code} — {r.name}": int(r.id)
                for r in active_products.itertuples()
            }
            selected = st.selectbox(
                "Produit", list(product_map.keys()), key="edit_product_sel"
            )
            selected_id = product_map[selected]
            selected_row = active_products[active_products["id"] == selected_id].iloc[0]

            category_index = (
                PRODUCT_CATEGORIES.index(selected_row["category"])
                if selected_row["category"] in PRODUCT_CATEGORIES else 0
            )

            with st.form(f"edit_product_{selected_id}"):
                new_name = st.text_input("Désignation", value=selected_row["name"])
                new_category = st.selectbox(
                    "Catégorie", PRODUCT_CATEGORIES, index=category_index
                )
                new_minimum = st.number_input(
                    "Seuil de stock bas",
                    min_value=0.0,
                    value=float(selected_row["minimum_stock"]),
                )
                save = st.form_submit_button("Enregistrer", type="primary")

            if save:
                if not new_name.strip():
                    st.error("La désignation est obligatoire.")
                else:
                    with get_db() as conn:
                        conn.execute(
                            """
                            UPDATE products
                            SET name = ?, category = ?, minimum_stock = ?
                            WHERE id = ?
                            """,
                            (new_name.strip(), new_category, new_minimum, selected_id),
                        )
                        _audit(
                            conn, "PRODUCT", selected_id, "UPDATE",
                            f"seuil={selected_row['minimum_stock']}",
                            f"{new_name.strip()} | seuil={new_minimum}", user,
                        )
                    flash_success("Produit modifié.")
                    st.rerun()

    delete_box(
        "🗑️ Supprimer un produit",
        {
            f"{r.code} — {r.name}": int(r.id)
            for r in active_products.itertuples()
        } if not active_products.empty else {},
        "del_product",
        delete_product,
        user,
        warning="Un produit encore en stock ne peut pas être supprimé. "
                "S'il a un historique de flux, il sera archivé.",
    )


def stock_management(user: Dict[str, Any]):
    st.subheader("Stocks par dépôt")

    depots = get_depots(user)

    if depots.empty:
        st.warning("Aucun dépôt disponible pour votre profil.")
        return

    depot_filter = {
        "Tous les dépôts": None,
        **{f"{r.name} — {r.cic_name}": int(r.id) for r in depots.itertuples()},
    }

    visible_products = get_products(user)
    product_filter = {
        "Tous les produits": None,
        **{f"{r.code} — {r.name}": int(r.id) for r in visible_products.itertuples()},
    }

    col1, col2 = st.columns(2)
    with col1:
        selected_depot = st.selectbox("Dépôt", list(depot_filter.keys()))
    with col2:
        selected_product = st.selectbox("Produit", list(product_filter.keys()))

    stock = get_stock_dataframe(
        user,
        depot_id=depot_filter[selected_depot],
        product_id=product_filter[selected_product],
    )

    if stock.empty:
        st.info("Aucun stock.")
    else:
        st.dataframe(
            stock[
                [
                    "depot_name", "cic_name", "product_code", "product_name",
                    "category", "quantity", "unit", "minimum_stock",
                    "stock_status", "updated_at",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
        render_low_stock_alerts(stock)

    # --- Mise à jour du stock (Admin / chef de dépôt) -----------------------
    if not can_adjust_stock(user):
        st.info(
            "Mode consultation : seul le chef de dépôt (pour son dépôt) "
            "ou l'Admin peut mettre à jour le stock."
        )
        return

    st.markdown("### Mise à jour du stock")

    if is_admin(user):
        adj_labels = {
            f"{r.name} — {r.cic_name}": int(r.id) for r in depots.itertuples()
        }
        adj_depot_label = st.selectbox(
            "Dépôt à mettre à jour", list(adj_labels.keys()), key="adj_depot"
        )
        adj_depot_id = adj_labels[adj_depot_label]
        adj_products = get_products(None)  # Admin : tout le catalogue
    else:
        adj_depot_id = user.get("depot_id")
        if not adj_depot_id:
            st.warning("Aucun dépôt ne vous est rattaché.")
            return
        st.info(f"Dépôt : **{depots.iloc[0]['name']}**")
        adj_products = get_products(user)

    if adj_products.empty:
        st.warning("Aucun produit disponible.")
        return

    adj_product_map = {
        f"{r.code} — {r.name}": int(r.id) for r in adj_products.itertuples()
    }
    adj_product_label = st.selectbox(
        "Produit", list(adj_product_map.keys()), key="adj_product"
    )
    adj_product_id = adj_product_map[adj_product_label]

    current_quantity = get_stock_quantity(adj_depot_id, adj_product_id)
    st.info(f"Stock actuel : {format_number(current_quantity)}")

    with st.form("stock_adjustment"):
        new_quantity = st.number_input(
            "Nouveau stock",
            min_value=0.0,
            value=float(current_quantity),
            step=10.0,
            key=f"newqty_{adj_depot_id}_{adj_product_id}",
        )
        save = st.form_submit_button("Enregistrer le stock", type="primary")

    if save:
        try:
            set_stock(user, adj_depot_id, adj_product_id, float(new_quantity))
        except (ValueError, PermissionError) as exc:
            st.error(str(exc))
            return

        flash_success("Stock mis à jour.")
        st.rerun()


def stock_import_panel(user: Dict[str, Any]):
    st.subheader("Importer le stock depuis Excel")

    if not can_adjust_stock(user):
        st.info("Mode consultation : l'import est réservé au chef de dépôt et à l'Admin.")
        return

    st.markdown(
        "Colonnes du fichier : **depot_code**, **product_code**, **quantity** "
        "(les codes sont ceux de l'application)."
    )
    if user["role"] == ROLE_AGENT:
        st.caption(
            "Chef de dépôt : l'import ne concerne que votre dépôt et les produits "
            "déjà présents chez vous. La colonne depot_code est facultative."
        )

    st.download_button(
        "⬇️ Télécharger le modèle Excel",
        data=build_stock_template(user),
        file_name="modele_import_stock.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    mode_labels = {
        "Remplacer le stock par les quantités du fichier": "replace",
        "Ajouter les quantités du fichier au stock existant": "add",
    }
    mode_label = st.radio("Mode d'import", list(mode_labels.keys()), key="imp_mode")
    mode = mode_labels[mode_label]

    default_depot_id = None
    if is_admin(user):
        depots = get_depots(None)
        options = {"— Utiliser la colonne depot_code du fichier —": None}
        options.update({f"{r.name} — {r.cic_name}": int(r.id) for r in depots.itertuples()})
        default_label = st.selectbox(
            "Dépôt par défaut (si la colonne depot_code est absente ou vide)",
            list(options.keys()),
            key="imp_default_depot",
        )
        default_depot_id = options[default_label]

    uploaded = st.file_uploader(
        "Fichier Excel (.xlsx) ou CSV", type=["xlsx", "csv"], key="imp_file"
    )
    if uploaded is None:
        return

    try:
        if uploaded.name.lower().endswith(".csv"):
            df = pd.read_csv(uploaded, sep=None, engine="python")
        else:
            df = pd.read_excel(uploaded, sheet_name=0)
        valid, errors = parse_stock_import(df, user, default_depot_id)
    except ImportError:
        st.error("Le module openpyxl est requis : pip install openpyxl")
        return
    except ValueError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.error(f"Fichier illisible : {exc}")
        return

    c1, c2 = st.columns(2)
    c1.metric("Lignes valides", len(valid))
    c2.metric("Lignes en erreur", len(errors))

    if errors:
        st.warning("Les lignes en erreur seront ignorées.")
        st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)

    if not valid:
        st.info("Aucune ligne valide à importer.")
        return

    preview = []
    for r in valid:
        current = get_stock_quantity(r["depot_id"], r["product_id"])
        new = r["quantity"] if mode == "replace" else current + r["quantity"]
        preview.append(
            {
                "Dépôt": r["depot_code"],
                "Produit": r["product_code"],
                "Stock actuel": current,
                "Quantité (fichier)": r["quantity"],
                "Nouveau stock": new,
            }
        )

    st.markdown("#### Aperçu avant import")
    st.dataframe(pd.DataFrame(preview), use_container_width=True, hide_index=True)

    if st.button(
        f"✅ Confirmer l'import ({len(valid)} ligne(s))",
        type="primary",
        key=f"imp_confirm_{uploaded.name}_{uploaded.size}",
    ):
        try:
            count = apply_stock_import(user, valid, mode)
        except (ValueError, PermissionError) as exc:
            st.error(str(exc))
            return

        flash_success(f"Import terminé : {count} ligne(s) de stock mise(s) à jour.")
        st.rerun()


def stocks_page(user: Dict[str, Any]):
    show_header(
        "Stocks & produits",
        "Niveaux de stock par dépôt, références et seuils d'alerte",
    )

    tab1, tab2, tab3 = st.tabs(["📦 Stocks", "🌾 Produits", "📥 Import Excel"])

    with tab1:
        stock_management(user)
    with tab2:
        products_management(user)
    with tab3:
        stock_import_panel(user)


# =============================================================================
# CIC & DEPOTS
# =============================================================================

def cics_tab(user: Dict[str, Any]):
    admin = is_admin(user)

    cics = get_cics(active_only=False)
    if not admin:
        cics = cics[cics["id"] == user.get("cic_id")]

    st.dataframe(cics, use_container_width=True, hide_index=True)

    if not admin:
        return

    with st.expander("➕ Ajouter un CIC"):
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
            submitted = st.form_submit_button("Ajouter le CIC", type="primary")

        if submitted:
            if not code.strip() or not name.strip():
                st.error("Le code et le nom sont obligatoires.")
            else:
                try:
                    with get_db() as conn:
                        cursor = conn.execute(
                            """
                            INSERT INTO cics
                            (code, name, complex_name, wilaya, address, manager_name)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (code.strip().upper(), name.strip(),
                             complex_name.strip() or name.strip(),
                             wilaya.strip(), address.strip(), manager_name.strip()),
                        )
                        _audit(conn, "CIC", cursor.lastrowid, "CREATE",
                               None, name.strip(), user)
                except sqlite3.IntegrityError:
                    st.error("Ce code CIC existe déjà.")
                else:
                    flash_success("CIC ajouté.")
                    st.rerun()

    delete_box(
        "🗑️ Supprimer un CIC",
        {f"{r.code} — {r.name}": int(r.id) for r in cics.itertuples()},
        "del_cic",
        delete_cic,
        user,
        warning="Un CIC ne peut être supprimé que s'il ne contient plus aucun dépôt "
                "ni utilisateur.",
    )


def depots_tab(user: Dict[str, Any]):
    admin = is_admin(user)

    depots = get_depots(user, active_only=False)

    if depots.empty:
        st.info("Aucun dépôt.")
    else:
        st.dataframe(depots.drop(columns=["cic_id"]), use_container_width=True,
                     hide_index=True)

    if not admin:
        return

    cics = get_cics()

    with st.expander("➕ Ajouter un dépôt"):
        if cics.empty:
            st.info("Créez d'abord un CIC.")
        else:
            cic_options = {r.name: int(r.id) for r in cics.itertuples()}
            with st.form("add_depot"):
                col1, col2 = st.columns(2)
                with col1:
                    code = st.text_input("Code dépôt", placeholder="DEP-SBA-03")
                    cic_name = st.selectbox("CIC de rattachement", list(cic_options.keys()))
                with col2:
                    name = st.text_input("Nom du dépôt")
                    address = st.text_input("Adresse")
                submitted = st.form_submit_button("Ajouter le dépôt", type="primary")

            if submitted:
                if not code.strip() or not name.strip():
                    st.error("Le code et le nom sont obligatoires.")
                else:
                    try:
                        with get_db() as conn:
                            cursor = conn.execute(
                                """
                                INSERT INTO depots (code, name, cic_id, address)
                                VALUES (?, ?, ?, ?)
                                """,
                                (code.strip().upper(), name.strip(),
                                 cic_options[cic_name], address.strip()),
                            )
                            _audit(conn, "DEPOT", cursor.lastrowid, "CREATE",
                                   None, name.strip(), user)
                    except sqlite3.IntegrityError:
                        st.error("Ce code dépôt existe déjà.")
                    else:
                        flash_success("Dépôt ajouté.")
                        st.rerun()

    delete_box(
        "🗑️ Supprimer un dépôt",
        {
            f"{r.code} — {r.name} ({r.cic_name})": int(r.id)
            for r in depots.itertuples() if int(r.active) == 1
        } if not depots.empty else {},
        "del_depot",
        delete_depot,
        user,
        warning="Le dépôt doit être vide (stock = 0), sans utilisateur ni flux en cours. "
                "S'il a un historique, il sera archivé.",
    )


def cics_page(user: Dict[str, Any]):
    show_header(
        "CIC & Dépôts",
        "Référentiel des centres, complexes et dépôts AGRODIV",
    )

    if user["role"] == ROLE_MANAGER:
        st.caption("Mode consultation : vous voyez votre CIC et ses dépôts.")

    tab1, tab2 = st.tabs(["🏢 CIC", "🏬 Dépôts"])

    with tab1:
        cics_tab(user)
    with tab2:
        depots_tab(user)


# =============================================================================
# AUDIT
# =============================================================================

def audit_page(user: Dict[str, Any]):
    show_header("Traçabilité & audit", "Journal des opérations et changements de statut")

    query = """
        SELECT id, created_at, entity_type, entity_id, action,
               old_value, new_value, username
        FROM audit_logs
    """
    params: List[Any] = []

    if user["role"] != ROLE_ADMIN:
        query += " WHERE user_id = ?"
        params.append(user["id"])

    query += " ORDER BY id DESC LIMIT 2000"

    with get_db() as conn:
        logs = pd.read_sql_query(query, conn, params=params)

    if logs.empty:
        st.info("Aucun événement d'audit.")
        return

    st.metric("Événements affichés", len(logs))
    st.dataframe(logs, use_container_width=True, hide_index=True)

    dataframe_download_button(
        logs,
        f"agrodiv_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        "⬇️ Exporter l'audit CSV",
    )


# =============================================================================
# USER MANAGEMENT
# =============================================================================

def profile_page(user: Dict[str, Any]):
    show_header("Mon mot de passe", "Modifier le mot de passe de mon compte")

    with st.form("change_own_password"):
        old_password = st.text_input("Mot de passe actuel", type="password")
        new_password = st.text_input("Nouveau mot de passe", type="password")
        confirm = st.text_input("Confirmer le nouveau mot de passe", type="password")
        submitted = st.form_submit_button("Changer le mot de passe", type="primary")

    if submitted:
        if new_password != confirm:
            st.error("La confirmation ne correspond pas au nouveau mot de passe.")
            return
        try:
            set_user_password(user["id"], new_password, user, old_password)
        except (ValueError, PermissionError) as exc:
            st.error(str(exc))
            return
        flash_success("Votre mot de passe a été modifié.")
        st.rerun()


def users_page(user: Dict[str, Any]):
    show_header(
        "Gestion des utilisateurs",
        "Administration des comptes, rôles, CIC et dépôts",
    )

    with get_db() as conn:
        users = pd.read_sql_query(
            """
            SELECT
                u.id, u.username, u.full_name, u.role,
                c.name AS cic, d.name AS depot, u.active, u.created_at
            FROM users u
            LEFT JOIN cics c ON c.id = u.cic_id
            LEFT JOIN depots d ON d.id = u.depot_id
            ORDER BY u.id
            """,
            conn,
        )

    st.dataframe(users, use_container_width=True, hide_index=True)

    st.subheader("Créer un utilisateur")
    st.caption(
        "Manager CIC : choisir un CIC (consultation de ses dépôts). "
        "Agent Commercial = chef de dépôt : choisir son dépôt."
    )

    cics = get_cics()
    depots = get_depots(None)

    cic_options = {"Aucun CIC": None, **{r.name: int(r.id) for r in cics.itertuples()}}
    depot_options = {
        "Aucun dépôt": None,
        **{f"{r.name} — {r.cic_name}": (int(r.id), int(r.cic_id)) for r in depots.itertuples()},
    }

    with st.form("new_user"):
        username = st.text_input("Nom d'utilisateur")
        full_name = st.text_input("Nom complet")
        role = st.selectbox("Rôle", ROLES)
        password = st.text_input("Mot de passe initial", type="password")
        cic_name = st.selectbox("CIC (pour Manager CIC)", list(cic_options.keys()))
        depot_label = st.selectbox("Dépôt (pour Agent Commercial)", list(depot_options.keys()))
        active = st.checkbox("Compte actif", value=True)
        submitted = st.form_submit_button("Créer le compte", type="primary")

    if submitted:
        cic_id = cic_options[cic_name]
        depot_id = None

        if not username.strip() or not full_name.strip():
            st.error("Nom d'utilisateur et nom complet obligatoires.")
            return
        if len(password) < 6:
            st.error("Le mot de passe doit contenir au moins 6 caractères.")
            return

        if role == ROLE_MANAGER:
            if cic_id is None:
                st.error("Un Manager CIC doit être rattaché à un CIC.")
                return
        elif role == ROLE_AGENT:
            if depot_options[depot_label] is None:
                st.error("Un Agent Commercial (chef de dépôt) doit être rattaché à un dépôt.")
                return
            depot_id, cic_id = depot_options[depot_label]
        else:
            cic_id = None

        try:
            with get_db() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users
                    (username, full_name, password_hash, role, cic_id, depot_id, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (username.strip(), full_name.strip(), hash_password(password),
                     role, cic_id, depot_id, 1 if active else 0),
                )
                _audit(conn, "USER", cursor.lastrowid, "CREATE",
                       None, username.strip(), user)
        except sqlite3.IntegrityError:
            st.error("Ce nom d'utilisateur existe déjà.")
            return

        flash_success("Utilisateur créé.")
        st.rerun()

    with st.expander("🔑 Changer le mot de passe d'un utilisateur"):
        reset_options = {
            f"{r.username} — {r.full_name}": int(r.id)
            for r in users.itertuples() if int(r.id) != user["id"]
        }
        if not reset_options:
            st.info("Aucun autre utilisateur.")
        else:
            with st.form("reset_password"):
                target_label = st.selectbox("Utilisateur", list(reset_options.keys()))
                new_pwd = st.text_input("Nouveau mot de passe", type="password")
                confirm_pwd = st.text_input("Confirmer le mot de passe", type="password")
                reset_submit = st.form_submit_button("Changer le mot de passe", type="primary")

            if reset_submit:
                if new_pwd != confirm_pwd:
                    st.error("La confirmation ne correspond pas.")
                else:
                    try:
                        message = set_user_password(
                            reset_options[target_label], new_pwd, user
                        )
                    except (ValueError, PermissionError) as exc:
                        st.error(str(exc))
                    else:
                        flash_success(message)
                        st.rerun()
        st.caption("Pour votre propre mot de passe, utilisez la page « Mon mot de passe ».")

    delete_box(
        "🗑️ Supprimer un utilisateur",
        {
            f"{r.username} — {r.full_name} ({r.role})": int(r.id)
            for r in users.itertuples() if int(r.id) != user["id"]
        },
        "del_user",
        delete_user,
        user,
        warning="Si l'utilisateur a un historique de flux, son compte sera désactivé "
                "au lieu d'être supprimé.",
    )


# =============================================================================
# MAIN
# =============================================================================

def get_page_icon():
    """Use logo.png as browser-tab icon (fallback: emoji)."""
    try:
        from PIL import Image
        return Image.open(LOGO_PATH)
    except Exception:
        return "🌾"


def main():
    st.set_page_config(
        page_title="AGRODIV — Flux commerciaux CIC",
        page_icon=get_page_icon(),
        layout="wide",
        initial_sidebar_state="expanded",
    )

    apply_css()
    init_database()

    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        show_flash_messages()
        login_page()
        return

    # Recharger l'utilisateur à chaque exécution (rôle / dépôt / compte actif)
    session_user = st.session_state.get("user")
    user = get_user(session_user["id"]) if session_user else None

    if not user:
        st.session_state.clear()
        st.rerun()
        return

    st.session_state["user"] = user

    selected_page = render_sidebar(user)
    show_flash_messages()

    if selected_page == PAGE_DASH:
        dashboard_page(user)
    elif selected_page == PAGE_FLUX:
        transfers_page(user)
    elif selected_page == PAGE_STOCK:
        stocks_page(user)
    elif selected_page == PAGE_CIC:
        if user["role"] in (ROLE_ADMIN, ROLE_MANAGER):
            cics_page(user)
        else:
            st.error("Accès refusé.")
    elif selected_page == PAGE_AUDIT:
        audit_page(user)
    elif selected_page == PAGE_PROFILE:
        profile_page(user)
    elif selected_page == PAGE_USERS:
        if is_admin(user):
            users_page(user)
        else:
            st.error("Accès refusé.")
    else:
        dashboard_page(user)


if __name__ == "__main__":
    main()
