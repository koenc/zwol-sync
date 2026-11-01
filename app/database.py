import logging
import sqlite3
import datetime
from pathlib import Path
from contextlib import contextmanager
from app.config import Config

logger = logging.getLogger("zwol_sync")

_USE_SQLITE = False


def check_mariadb_connection():
    """Controleert of MariaDB bereikbaar is, anders schakelen we over op SQLite."""
    global _USE_SQLITE
    if _USE_SQLITE:
        return False
    if not Config.DB_HOST:
        _USE_SQLITE = True
        return False

    try:
        import pymysql
        conn = pymysql.connect(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=Config.DB_NAME,
            connect_timeout=3,
            cursorclass=pymysql.cursors.DictCursor
        )
        conn.close()
        _USE_SQLITE = False
        logger.info(f"✅ Succesvol verbonden met MariaDB database '{Config.DB_NAME}' op {Config.DB_HOST}:{Config.DB_PORT}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ Kon geen verbinding maken met MariaDB ({e}). Overschakelen op lokale SQLite fallback.")
        _USE_SQLITE = True
        return False


class DatabaseConnection:
    """Wrapper die zowel PyMySQL als SQLite uniforme methoden biedt."""
    def __init__(self, conn, is_sqlite=False):
        self.conn = conn
        self.is_sqlite = is_sqlite

    def execute(self, query, params=None):
        if self.is_sqlite:
            sqlite_query = query.replace("%s", "?")
            cur = self.conn.cursor()
            cur.execute(sqlite_query, params or ())
            return cur
        else:
            cur = self.conn.cursor()
            cur.execute(query, params or ())
            return cur

    def executemany(self, query, params_list):
        if self.is_sqlite:
            sqlite_query = query.replace("%s", "?")
            cur = self.conn.cursor()
            cur.executemany(sqlite_query, params_list)
            return cur
        else:
            cur = self.conn.cursor()
            cur.executemany(query, params_list)
            return cur

    def fetchall(self, query, params=None):
        cur = self.execute(query, params)
        if self.is_sqlite:
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        else:
            return cur.fetchall()

    def fetchone(self, query, params=None):
        cur = self.execute(query, params)
        if self.is_sqlite:
            row = cur.fetchone()
            return dict(row) if row else None
        else:
            return cur.fetchone()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


@contextmanager
def get_db():
    """Context manager voor veilige database interactie."""
    global _USE_SQLITE

    if not _USE_SQLITE and Config.DB_HOST:
        conn = None
        try:
            import pymysql
            conn = pymysql.connect(
                host=Config.DB_HOST,
                port=Config.DB_PORT,
                user=Config.DB_USER,
                password=Config.DB_PASSWORD,
                database=Config.DB_NAME,
                connect_timeout=5,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False
            )
            db = DatabaseConnection(conn, is_sqlite=False)
        except Exception as e:
            logger.warning(f"MariaDB connectiefout: {e}. Fallback naar SQLite.")
            _USE_SQLITE = True

        if conn is not None:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            return

    # SQLite fallback
    Config.SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(Config.SQLITE_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    db_wrapper = DatabaseConnection(conn, is_sqlite=True)
    try:
        yield db_wrapper
        db_wrapper.commit()
    except Exception:
        db_wrapper.rollback()
        raise
    finally:
        db_wrapper.close()


def init_db():
    """Initialiseert de databasetabellen voor ZWOL Sync."""
    check_mariadb_connection()
    prefix = Config.DB_TABLE_PREFIX

    with get_db() as db:
        if db.is_sqlite:
            # SQLite DDL
            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}silvasoft_cache (
                customer_number TEXT PRIMARY KEY,
                relation_guid TEXT,
                name TEXT NOT NULL,
                kvk_number TEXT,
                street TEXT,
                unit TEXT,
                unit_appendix TEXT,
                postal_code TEXT,
                city TEXT,
                phone TEXT,
                email TEXT,
                website TEXT,
                contacts_json TEXT,
                primary_contact_name TEXT,
                primary_contact_email TEXT,
                primary_contact_phone TEXT,
                raw_json TEXT,
                synced_at TEXT
            );
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}hubspot_cache (
                hubspot_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                domain TEXT,
                website TEXT,
                kvk_number TEXT,
                street TEXT,
                postal_code TEXT,
                city TEXT,
                phone TEXT,
                company_type TEXT,
                silvasoft_customer_number TEXT,
                contacts_json TEXT,
                primary_contact_name TEXT,
                primary_contact_email TEXT,
                primary_contact_phone TEXT,
                raw_json TEXT,
                synced_at TEXT
            );
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                silvasoft_customer_number TEXT UNIQUE NOT NULL,
                hubspot_id TEXT NOT NULL,
                match_method TEXT NOT NULL,
                confidence_score INTEGER DEFAULT 100,
                is_confirmed INTEGER DEFAULT 1,
                matched_at TEXT NOT NULL
            );
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                payload_json TEXT,
                status TEXT DEFAULT 'PENDING',
                attempts INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                processed_at TEXT
            );
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_name TEXT NOT NULL,
                target_system TEXT NOT NULL,
                change_summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_type TEXT NOT NULL,
                status TEXT NOT NULL,
                records_count INTEGER DEFAULT 0,
                message TEXT,
                created_at TEXT NOT NULL
            );
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}ignored_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_number TEXT NOT NULL,
                hubspot_id TEXT NOT NULL,
                contact_id TEXT NOT NULL,
                contact_name TEXT,
                contact_email TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (customer_number, contact_id)
            );
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix}hubspot_company_types (
                type_value TEXT PRIMARY KEY,
                type_label TEXT NOT NULL,
                display_order INTEGER DEFAULT 0,
                is_hidden INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """)
        else:
            # MariaDB DDL
            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}silvasoft_cache` (
                `customer_number` VARCHAR(50) NOT NULL,
                `relation_guid` VARCHAR(100) NULL,
                `name` VARCHAR(255) NOT NULL,
                `kvk_number` VARCHAR(50) NULL,
                `street` VARCHAR(255) NULL,
                `unit` VARCHAR(50) NULL,
                `unit_appendix` VARCHAR(20) NULL,
                `postal_code` VARCHAR(20) NULL,
                `city` VARCHAR(100) NULL,
                `phone` VARCHAR(50) NULL,
                `email` VARCHAR(255) NULL,
                `website` VARCHAR(255) NULL,
                `contacts_json` LONGTEXT NULL,
                `primary_contact_name` VARCHAR(255) NULL,
                `primary_contact_email` VARCHAR(255) NULL,
                `primary_contact_phone` VARCHAR(50) NULL,
                `raw_json` LONGTEXT NULL,
                `synced_at` DATETIME NULL,
                PRIMARY KEY (`customer_number`),
                INDEX `idx_sc_name` (`name`),
                INDEX `idx_sc_kvk` (`kvk_number`),
                INDEX `idx_sc_zip` (`postal_code`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}hubspot_cache` (
                `hubspot_id` VARCHAR(50) NOT NULL,
                `name` VARCHAR(255) NOT NULL,
                `domain` VARCHAR(255) NULL,
                `website` VARCHAR(255) NULL,
                `kvk_number` VARCHAR(50) NULL,
                `street` VARCHAR(255) NULL,
                `postal_code` VARCHAR(20) NULL,
                `city` VARCHAR(100) NULL,
                `phone` VARCHAR(50) NULL,
                `company_type` VARCHAR(100) NULL,
                `silvasoft_customer_number` VARCHAR(50) NULL,
                `contacts_json` LONGTEXT NULL,
                `primary_contact_name` VARCHAR(255) NULL,
                `primary_contact_email` VARCHAR(255) NULL,
                `primary_contact_phone` VARCHAR(50) NULL,
                `raw_json` LONGTEXT NULL,
                `synced_at` DATETIME NULL,
                PRIMARY KEY (`hubspot_id`),
                INDEX `idx_hc_name` (`name`),
                INDEX `idx_hc_kvk` (`kvk_number`),
                INDEX `idx_hc_custno` (`silvasoft_customer_number`),
                INDEX `idx_hc_zip` (`postal_code`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}matches` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `silvasoft_customer_number` VARCHAR(50) NOT NULL UNIQUE,
                `hubspot_id` VARCHAR(50) NOT NULL,
                `match_method` VARCHAR(50) NOT NULL,
                `confidence_score` INT DEFAULT 100,
                `is_confirmed` TINYINT(1) DEFAULT 1,
                `matched_at` DATETIME NOT NULL,
                INDEX `idx_match_hs` (`hubspot_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}queue` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `target` VARCHAR(20) NOT NULL,
                `action` VARCHAR(50) NOT NULL,
                `entity_id` VARCHAR(50) NOT NULL,
                `field_name` VARCHAR(50) NULL,
                `old_value` TEXT NULL,
                `new_value` TEXT NULL,
                `payload_json` LONGTEXT NULL,
                `status` VARCHAR(20) DEFAULT 'PENDING',
                `attempts` INT DEFAULT 0,
                `error_message` TEXT NULL,
                `created_at` DATETIME NOT NULL,
                `scheduled_at` DATETIME NOT NULL,
                `processed_at` DATETIME NULL,
                INDEX `idx_queue_status` (`status`, `target`, `scheduled_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}history` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `entity_name` VARCHAR(255) NOT NULL,
                `target_system` VARCHAR(20) NOT NULL,
                `change_summary` TEXT NOT NULL,
                `created_at` DATETIME NOT NULL,
                INDEX `idx_hist_date` (`created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}sync_log` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `sync_type` VARCHAR(50) NOT NULL,
                `status` VARCHAR(20) NOT NULL,
                `records_count` INT DEFAULT 0,
                `message` TEXT NULL,
                `created_at` DATETIME NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}ignored_contacts` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `customer_number` VARCHAR(50) NOT NULL,
                `hubspot_id` VARCHAR(50) NOT NULL,
                `contact_id` VARCHAR(50) NOT NULL,
                `contact_name` VARCHAR(255) NULL,
                `contact_email` VARCHAR(255) NULL,
                `created_at` DATETIME NOT NULL,
                UNIQUE KEY `uq_ignored_contact` (`customer_number`, `contact_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

            db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{prefix}hubspot_company_types` (
                `type_value` VARCHAR(100) NOT NULL,
                `type_label` VARCHAR(255) NOT NULL,
                `display_order` INT DEFAULT 0,
                `is_hidden` TINYINT DEFAULT 0,
                `updated_at` DATETIME NOT NULL,
                PRIMARY KEY (`type_value`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)

        # Migraties voor bestaande tabellen
        try:
            if db.is_sqlite:
                db.execute(f"ALTER TABLE {prefix}hubspot_cache ADD COLUMN company_type TEXT;")
            else:
                db.execute(f"ALTER TABLE `{prefix}hubspot_cache` ADD COLUMN `company_type` VARCHAR(100) NULL;")
        except Exception:
            pass  # Kolom bestaat al

    logger.info(f"Database tabellen met prefix '{prefix}' succesvol gecontroleerd/aangemaakt.")

