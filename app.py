import copy
import json
import mimetypes
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

import click
from flask import (
    Flask,
    abort,
    flash,
    get_flashed_messages,
    has_request_context,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
Path(app.instance_path).mkdir(parents=True, exist_ok=True)
secret_key_file = Path(app.instance_path) / "secret_key"
if os.environ.get("FLASK_SECRET_KEY"):
    app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]
else:
    if not secret_key_file.exists():
        secret_key_file.write_text(secrets.token_hex(32), encoding="ascii")
    app.config["SECRET_KEY"] = secret_key_file.read_text(encoding="ascii").strip()
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017/").strip()
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "dulce_encanto").strip()
try:
    MONGODB_TIMEOUT_MS = int(os.environ.get("MONGODB_TIMEOUT_MS", "800"))
    RESERVATION_SECONDS = int(os.environ.get("RESERVATION_SECONDS", "60"))
except ValueError as error:
    raise RuntimeError("MONGODB_TIMEOUT_MS debe ser un número entero positivo.") from error

FALLBACK_ADMIN_EMAIL = "Admin"
FALLBACK_ADMIN_PASSWORD = "admin123"

if not MONGODB_DATABASE:
    raise RuntimeError("MONGODB_DATABASE no puede estar vacío.")
if MONGODB_TIMEOUT_MS <= 0:
    raise RuntimeError("MONGODB_TIMEOUT_MS debe ser un número entero positivo.")

PRODUCTS_FILE = Path(app.instance_path) / "products.json"
PRODUCT_IMAGES_DIRECTORY = Path(app.instance_path) / "product_images"
_mongo_client: MongoClient | None = None
_mongo_initialized = False
_mongo_available = False
_fallback_store: dict[str, list[dict]] = {}
COLLECTION_NAMES = ("users", "products", "sales", "purchases", "inventory", "inventory_movements")
PRODUCT_CATEGORIES = {
    "pasteles": "Pasteles",
    "panes": "Panes",
    "postres": "Postres",
    "bebidas": "Bebidas",
}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

DEFAULT_PRODUCTS = [
    {
        "id": "default-panqueque",
        "name": "Panqueque",
        "category": "panes",
        "category_label": "Dulce clásico",
        "description": "Un antojo delicioso para disfrutar a cualquier hora.",
        "image": "images (1).jfif",
        "tag": "Favorito",
        "price_cents": 29900,
        "uploaded": False,
        "active": True,
    },
    {
        "id": "default-delicia-casa",
        "name": "Delicia de la casa",
        "category": "pasteles",
        "category_label": "Una dulce sorpresa",
        "description": "Una opción especial para consentirte con algo rico.",
        "image": "images.jfif",
        "tag": "Recién hecho",
        "price_cents": None,
        "uploaded": False,
        "active": True,
    },
    {
        "id": "default-pastel-frutos-rojos",
        "name": "Pastel de frutos rojos",
        "category": "pasteles",
        "category_label": "Pasteles",
        "description": "Pastel decorado con fresas y frutos rojos frescos.",
        "image": "images (3).jfif",
        "tag": "Especial de la casa",
        "price_cents": None,
        "uploaded": False,
        "active": True,
    },
    {
        "id": "default-capricho-artesanal",
        "name": "Capricho artesanal",
        "category": "postres",
        "category_label": "Un toque de felicidad",
        "description": "El acompañamiento perfecto para tu pausa favorita.",
        "image": "images (2).jfif",
        "tag": "Para compartir",
        "price_cents": None,
        "uploaded": False,
        "active": True,
    },
]


class _FallbackCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, field, direction):
        reverse = direction in (DESCENDING, -1)
        self.docs = sorted(self.docs, key=lambda item: item.get(field) or 0, reverse=reverse)
        return self

    def __iter__(self):
        return iter(self.docs)

    def __len__(self):
        return len(self.docs)


class _FallbackCollection:
    def __init__(self, name, initial=None):
        self.name = name
        self.docs = list(initial or [])

    @staticmethod
    def _value_at(doc, key):
        if "." not in key:
            return doc.get(key)
        value = doc
        for part in key.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    @classmethod
    def _match_item(cls, doc, criteria):
        if criteria is None or criteria == {}:
            return True
        for key, expected in criteria.items():
            if key == "$and":
                if not all(cls._match_item(doc, item) for item in expected):
                    return False
                continue

            actual = cls._value_at(doc, key)
            if isinstance(expected, dict):
                if "$in" in expected:
                    if actual not in expected["$in"]:
                        return False
                elif "$gt" in expected:
                    if actual is None or not actual > expected["$gt"]:
                        return False
                elif "$gte" in expected:
                    if actual is None or not actual >= expected["$gte"]:
                        return False
                elif "$lt" in expected:
                    if actual is None or not actual < expected["$lt"]:
                        return False
                elif "$lte" in expected:
                    if actual is None or not actual <= expected["$lte"]:
                        return False
                elif "$ne" in expected:
                    if actual == expected["$ne"]:
                        return False
                else:
                    return False
            elif actual != expected:
                return False
        return True

    def find_one(self, filter=None, projection=None):
        for item in self.docs:
            if self._match_item(item, filter or {}):
                return copy.deepcopy(item)
        return None

    def find(self, filter=None, projection=None):
        return _FallbackCursor(item for item in self.docs if self._match_item(item, filter or {}))

    def insert_one(self, document):
        self.docs.append(copy.deepcopy(document))
        return type("InsertResult", (), {"inserted_id": document.get("id")})()

    def update_one(self, filter, update):
        matched = False
        for item in self.docs:
            if self._match_item(item, filter or {}):
                matched = True
                if "$set" in update:
                    item.update(update["$set"])
                if "$inc" in update:
                    for key, value in update["$inc"].items():
                        current = item.get(key, 0)
                        item[key] = current + value
                if "$unset" in update:
                    for key in update["$unset"]:
                        item.pop(key, None)
        return type("UpdateResult", (), {"matched_count": 1 if matched else 0, "modified_count": 1 if matched else 0})()

    def update_many(self, filter, update):
        result = self.update_one(filter, update)
        return result

    def delete_one(self, filter):
        for index, item in enumerate(self.docs):
            if self._match_item(item, filter or {}):
                del self.docs[index]
                return type("DeleteResult", (), {"deleted_count": 1})()
        return type("DeleteResult", (), {"deleted_count": 0})()

    def count_documents(self, filter=None):
        return sum(1 for item in self.docs if self._match_item(item, filter or {}))

    def aggregate(self, pipeline):
        return list(self.docs)

    def create_index(self, index):
        return None

    def find_one_and_update(self, filter, update, return_document=None):
        for item in self.docs:
            if self._match_item(item, filter or {}):
                if "$set" in update:
                    item.update(update["$set"])
                if "$inc" in update:
                    for key, value in update["$inc"].items():
                        item[key] = item.get(key, 0) + value
                return copy.deepcopy(item)
        return None


def _reset_fallback_store():
    global _fallback_store
    now = datetime.now(timezone.utc)
    products = []
    for product in DEFAULT_PRODUCTS:
        products.append({**product, "created_at": now})
    inventory = []
    for product in products:
        inventory.append({"product_id": product["id"], "quantity": 0, "updated_at": now})
    users = [{
        "id": "fallback-admin",
        "email": FALLBACK_ADMIN_EMAIL,
        "password_hash": generate_password_hash(FALLBACK_ADMIN_PASSWORD),
        "role": "admin",
        "active": True,
        "created_at": now,
    }]
    _fallback_store = {
        "users": users,
        "products": products,
        "sales": [],
        "purchases": [],
        "inventory": inventory,
        "inventory_movements": [],
    }


def inicializar_base_datos() -> None:
    global _mongo_client, _mongo_initialized, _mongo_available
    if _mongo_initialized:
        return
    try:
        if _mongo_client is None:
            _mongo_client = MongoClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=MONGODB_TIMEOUT_MS,
                connectTimeoutMS=MONGODB_TIMEOUT_MS,
                socketTimeoutMS=MONGODB_TIMEOUT_MS,
            )
        _mongo_client.admin.command("ping")
        database = _mongo_client[MONGODB_DATABASE]
        existing_collections = set(database.list_collection_names())
        for name in COLLECTION_NAMES:
            if name not in existing_collections:
                database.create_collection(name)

        database["users"].create_index([("email", ASCENDING)], unique=True)
        database["products"].create_index([("id", ASCENDING)], unique=True)
        database["products"].create_index([("category", ASCENDING), ("active", ASCENDING)])
        database["products"].create_index([("active", ASCENDING), ("created_at", ASCENDING)])
        database["sales"].create_index([("id", ASCENDING)], unique=True)
        database["sales"].create_index([("created_at", DESCENDING)])
        database["sales"].create_index([("customer_email", ASCENDING)])
        database["purchases"].create_index([("id", ASCENDING)], unique=True)
        database["purchases"].create_index([("created_at", DESCENDING)])
        database["inventory"].create_index([("product_id", ASCENDING)], unique=True)
        database["inventory_movements"].create_index([("id", ASCENDING)], unique=True)
        database["inventory_movements"].create_index([("product_id", ASCENDING), ("created_at", DESCENDING)])

        now = datetime.now(timezone.utc)
        for product in DEFAULT_PRODUCTS:
            database["products"].update_one(
                {"id": product["id"]},
                {"$setOnInsert": {**product, "created_at": now}},
                upsert=True,
            )
            database["inventory"].update_one(
                {"product_id": product["id"]},
                {"$setOnInsert": {"product_id": product["id"], "quantity": 0, "updated_at": now}},
                upsert=True,
            )
        _mongo_available = True
    except Exception:
        _mongo_client = None
        _mongo_available = False
        _reset_fallback_store()
    finally:
        _mongo_initialized = True


def obtener_coleccion(nombre: str):
    if nombre not in COLLECTION_NAMES:
        raise ValueError(f"Colección no configurada: {nombre}")
    inicializar_base_datos()
    if not _mongo_available:
        return _FallbackCollection(nombre, _fallback_store.get(nombre, []))
    return _mongo_client[MONGODB_DATABASE][nombre]


def obtener_coleccion_productos() -> Collection:
    return obtener_coleccion("products")


def normalizar_fecha_hora(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def revisar_reservas_vencidas() -> int:
    ahora = datetime.now(timezone.utc)
    pendientes = list(
        obtener_coleccion("sales").find(
            {"status": "pending", "expires_at": {"$lte": ahora}},
            {"_id": 0, "id": 1, "expires_at": 1},
        )
    )
    pendientes = [
        pedido for pedido in pendientes if normalizar_fecha_hora(pedido.get("expires_at")) is not None and normalizar_fecha_hora(pedido["expires_at"]) <= ahora
    ]
    cancelled_by = session.get("user_id") if has_request_context() else None
    for pedido in pendientes:
        obtener_coleccion("sales").update_one(
            {"id": pedido["id"], "status": "pending"},
            {
                "$set": {
                    "status": "cancelled",
                    "cancelled_at": ahora,
                    "cancelled_by": cancelled_by,
                    "reservation_expired": True,
                }
            },
        )
    return len(pendientes)


def cargar_productos():
    ahora = datetime.now(timezone.utc)
    products = list(
        obtener_coleccion_productos()
        .find({"active": True}, {"_id": 0, "photo": 0, "photo_mimetype": 0})
        .sort("created_at", ASCENDING)
    )
    product_ids = [product["id"] for product in products]
    inventory = obtener_coleccion("inventory").find(
        {"product_id": {"$in": product_ids}},
        {"_id": 0, "product_id": 1, "quantity": 1},
    )
    quantities = {item["product_id"]: item["quantity"] for item in inventory}
    for product in products:
        product["quantity"] = quantities.get(product["id"], 0)
        reserva = obtener_coleccion("sales").find_one(
            {
                "status": "pending",
                "items.product_id": product["id"],
                "expires_at": {"$gt": ahora},
            },
            {"_id": 0, "expires_at": 1},
        )
        product["reserved"] = reserva is not None
        product["reservation_expires_at"] = normalizar_fecha_hora(reserva["expires_at"]) if reserva else None
    return products


def guardar_producto(producto, photo_data, photo_mimetype, initial_quantity):
    now = datetime.now(timezone.utc)
    mongo_producto = {
        **producto,
        "image": producto["id"],
        "uploaded": True,
        "photo": photo_data,
        "photo_mimetype": photo_mimetype,
        "created_at": now,
    }
    products = obtener_coleccion_productos()
    products.insert_one(mongo_producto)
    try:
        obtener_coleccion("inventory").insert_one(
            {"product_id": producto["id"], "quantity": initial_quantity, "updated_at": now}
        )
    except PyMongoError:
        products.delete_one({"id": producto["id"]})
        raise


def obtener_catalogo():
    return cargar_productos()


def obtener_carrito() -> list[dict]:
    carrito = session.get("cart")
    if not isinstance(carrito, list):
        session["cart"] = []
        return []

    limpio = []
    for item in carrito:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id")
        quantity = int(item.get("quantity", 1) or 1)
        if not product_id or quantity < 1:
            continue
        limpio.append({"product_id": product_id, "quantity": quantity})
    session["cart"] = limpio
    return limpio


def obtener_resumen_carrito():
    carrito = obtener_carrito()
    items = []
    total_cents = 0
    cantidad_total = 0
    for item in carrito:
        product = obtener_coleccion("products").find_one(
            {"id": item["product_id"], "active": True},
            {"_id": 0, "id": 1, "name": 1, "price_cents": 1},
        )
        if product is None:
            continue
        cantidad = item["quantity"]
        price = product.get("price_cents")
        if isinstance(price, int) and price > 0:
            total_cents += price * cantidad
        items.append(
            {
                "product_id": product["id"],
                "name": product["name"],
                "quantity": cantidad,
                "price_cents": price,
                "line_total_cents": (price or 0) * cantidad,
            }
        )
        cantidad_total += cantidad
    return {"items": items, "count": cantidad_total, "total_cents": total_cents}


def obtener_reservas_activas():
    ids = session.get("recent_reservations") or []
    if not isinstance(ids, list):
        ids = []
    ids = [str(item) for item in ids if item]

    customer_email = ""
    session_email = session.get("last_customer_email")
    if isinstance(session_email, str) and session_email.strip():
        customer_email = session_email.strip().casefold()

    ahora = datetime.now(timezone.utc)
    query = {"status": "pending", "expires_at": {"$gt": ahora}}
    if ids:
        query["id"] = {"$in": ids}
    elif customer_email:
        query["customer_email"] = customer_email
    else:
        return []

    reservas = list(
        obtener_coleccion("sales")
        .find(query, {"_id": 0})
        .sort("created_at", DESCENDING)
    )
    for reserva in reservas:
        expires_at = normalizar_fecha_hora(reserva.get("expires_at"))
        if expires_at is None:
            reserva["remaining_seconds"] = 0
            continue
        remaining_seconds = max(0, int((expires_at - ahora).total_seconds()))
        reserva["remaining_seconds"] = remaining_seconds
        reserva["product_name"] = reserva["items"][0]["product_name"] if reserva.get("items") else "Producto"
        reserva["expires_at"] = expires_at
    return reservas


def limpiar_id_reserva(order_id):
    ids = session.get("recent_reservations") or []
    if not isinstance(ids, list):
        return
    filtered = [item for item in ids if str(item) != str(order_id)]
    session["recent_reservations"] = filtered


def obtener_usuario_actual():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return obtener_coleccion("users").find_one(
        {"id": user_id, "active": True},
        {"_id": 0, "password_hash": 0},
    )


def es_administrador() -> bool:
    user = obtener_usuario_actual()
    return bool(user and user.get("role") == "admin")


def requerir_admin():
    if not es_administrador():
        abort(403)


def registrar_movimiento(product_id, quantity_delta, movement_type, reference_id):
    obtener_coleccion("inventory_movements").insert_one(
        {
            "id": uuid.uuid4().hex,
            "product_id": product_id,
            "quantity_delta": quantity_delta,
            "type": movement_type,
            "reference_id": reference_id,
            "created_at": datetime.now(timezone.utc),
            "created_by": session.get("user_id"),
        }
    )


def registrar_compra(product_id, quantity, unit_cost_cents, supplier):
    products = obtener_coleccion("products")
    product = products.find_one({"id": product_id, "active": True}, {"_id": 0, "name": 1})
    if product is None:
        raise ValueError("El producto seleccionado no existe.")

    purchase_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    purchase = {
        "id": purchase_id,
        "supplier": supplier,
        "items": [
            {
                "product_id": product_id,
                "product_name": product["name"],
                "quantity": quantity,
                "unit_cost_cents": unit_cost_cents,
            }
        ],
        "total_cents": quantity * unit_cost_cents,
        "created_at": now,
        "created_by": session["user_id"],
    }
    obtener_coleccion("purchases").insert_one(purchase)
    inventory_updated = False
    try:
        obtener_coleccion("inventory").update_one(
            {"product_id": product_id},
            {"$inc": {"quantity": quantity}, "$set": {"updated_at": now}},
            upsert=True,
        )
        inventory_updated = True
        registrar_movimiento(product_id, quantity, "purchase", purchase_id)
    except PyMongoError:
        if inventory_updated:
            obtener_coleccion("inventory").update_one(
                {"product_id": product_id, "quantity": {"$gte": quantity}},
                {"$inc": {"quantity": -quantity}, "$set": {"updated_at": datetime.now(timezone.utc)}},
            )
        obtener_coleccion("purchases").delete_one({"id": purchase_id})
        raise
    return purchase


def registrar_venta(product_id, quantity, customer_email):
    products = obtener_coleccion("products")
    product = products.find_one({"id": product_id, "active": True}, {"_id": 0})
    if product is None:
        raise ValueError("El producto seleccionado no existe.")
    if not isinstance(product.get("price_cents"), int) or product["price_cents"] <= 0:
        raise ValueError("Asigna un precio válido antes de registrar la venta.")
    inventory = obtener_coleccion("inventory")
    now = datetime.now(timezone.utc)
    updated_inventory = inventory.find_one_and_update(
        {"product_id": product_id, "quantity": {"$gte": quantity}},
        {"$inc": {"quantity": -quantity}, "$set": {"updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if updated_inventory is None:
        raise ValueError("No hay suficiente existencia para completar la venta.")

    sale_id = uuid.uuid4().hex
    unit_price_cents = product["price_cents"]
    sale = {
        "id": sale_id,
        "customer_email": customer_email,
        "items": [
            {
                "product_id": product_id,
                "product_name": product["name"],
                "quantity": quantity,
                "unit_price_cents": unit_price_cents,
            }
        ],
        "total_cents": quantity * unit_price_cents,
        "status": "completed",
        "created_at": now,
        "created_by": session["user_id"],
    }
    try:
        obtener_coleccion("sales").insert_one(sale)
        registrar_movimiento(product_id, -quantity, "sale", sale_id)
    except PyMongoError:
        inventory.update_one(
            {"product_id": product_id},
            {"$inc": {"quantity": quantity}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        obtener_coleccion("sales").delete_one({"id": sale_id})
        raise
    return sale


def registrar_solicitud_pedido(product_id, quantity, customer_name, customer_email, note):
    revisar_reservas_vencidas()

    product = obtener_coleccion("products").find_one(
        {"id": product_id, "active": True},
        {"_id": 0, "name": 1, "price_cents": 1},
    )
    if product is None:
        raise ValueError("Este producto ya no está disponible.")
    if not isinstance(quantity, int) or quantity < 1 or quantity > 100:
        raise ValueError("La cantidad debe estar entre 1 y 100.")
    stock = obtener_coleccion("inventory").find_one(
        {"product_id": product_id}, {"_id": 0, "quantity": 1}
    )
    if stock is None or stock.get("quantity", 0) < quantity:
        raise ValueError("No hay suficiente existencia para este producto.")
    if not customer_name or len(customer_name) > 100:
        raise ValueError("Escribe tu nombre (hasta 100 caracteres).")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer_email):
        raise ValueError("Escribe un correo electrónico válido.")
    if len(note) > 500:
        raise ValueError("La nota no puede superar los 500 caracteres.")

    now = datetime.now(timezone.utc)
    existing_reservation = obtener_coleccion("sales").find_one(
        {
            "status": "pending",
            "items.product_id": product_id,
            "expires_at": {"$gt": now},
        },
        {"_id": 0, "id": 1},
    )
    if existing_reservation:
        raise ValueError("Este producto ya está reservado. Elige otro o espera a que termine la reserva.")

    unit_price_cents = product.get("price_cents")
    if not isinstance(unit_price_cents, int) or unit_price_cents <= 0:
        unit_price_cents = None
    pedido = {
        "id": uuid.uuid4().hex,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "items": [
            {
                "product_id": product_id,
                "product_name": product["name"],
                "quantity": quantity,
                "unit_price_cents": unit_price_cents,
            }
        ],
        "total_cents": quantity * unit_price_cents if unit_price_cents else None,
        "status": "pending",
        "note": note,
        "created_at": now,
        "expires_at": now + timedelta(seconds=RESERVATION_SECONDS),
        "created_by": session.get("user_id"),
    }
    obtener_coleccion("sales").insert_one(pedido)
    recent = session.get("recent_reservations") or []
    if not isinstance(recent, list):
        recent = []
    if pedido["id"] not in recent:
        recent.append(pedido["id"])
    session["recent_reservations"] = recent[-12:]
    session["last_customer_email"] = customer_email
    return pedido


def confirmar_solicitud_pedido(order_id):
    sales = obtener_coleccion("sales")
    order = sales.find_one({"id": order_id, "status": "pending"}, {"_id": 0})
    if order is None:
        raise ValueError("La solicitud ya fue procesada o no existe.")
    expires_at = normalizar_fecha_hora(order.get("expires_at"))
    if expires_at and expires_at <= datetime.now(timezone.utc):
        sales.update_one(
            {"id": order_id, "status": "pending"},
            {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc), "reservation_expired": True}},
        )
        limpiar_id_reserva(order_id)
        raise ValueError("La reserva venció antes de confirmar la compra.")

    item = order["items"][0]
    if not isinstance(item.get("unit_price_cents"), int) or item["unit_price_cents"] <= 0:
        raise ValueError("Asigna primero un precio al producto desde la administración.")
    product_id = item["product_id"]
    quantity = item["quantity"]
    inventory = obtener_coleccion("inventory")
    updated_inventory = inventory.find_one_and_update(
        {"product_id": product_id, "quantity": {"$gte": quantity}},
        {"$inc": {"quantity": -quantity}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if updated_inventory is None:
        raise ValueError("No hay suficiente inventario para confirmar este pedido.")

    result = sales.update_one(
        {"id": order_id, "status": "pending"},
        {
            "$set": {
                "status": "completed",
                "total_cents": quantity * item["unit_price_cents"],
                "confirmed_at": datetime.now(timezone.utc),
                "confirmed_by": session.get("user_id"),
            }
        },
    )
    if result.modified_count != 1:
        inventory.update_one(
            {"product_id": product_id},
            {"$inc": {"quantity": quantity}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        raise ValueError("La solicitud ya fue procesada por otro administrador.")
    try:
        registrar_movimiento(product_id, -quantity, "sale", order_id)
    except PyMongoError:
        sales.update_one(
            {"id": order_id, "status": "completed"},
            {"$set": {"status": "pending"}, "$unset": {"total_cents": "", "confirmed_at": "", "confirmed_by": ""}},
        )
        inventory.update_one(
            {"product_id": product_id},
            {"$inc": {"quantity": quantity}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        raise
    return order


# =========================
# RUTAS
# =========================

@app.route("/")
def index():
    revisar_reservas_vencidas()
    user = obtener_usuario_actual()
    carrito = obtener_resumen_carrito()
    reservas_activas = obtener_reservas_activas()
    return render_template(
        "index.html",
        usuario_email=user.get("email") if user else None,
        es_admin=bool(user and user.get("role") == "admin"),
        productos=obtener_catalogo(),
        carrito=carrito,
        reservas_activas=reservas_activas,
        flashes=get_flashed_messages(with_categories=True),
    )


@app.route("/reserva/<order_id>/confirmar", methods=["POST"])
def confirmar_reserva_route(order_id):
    try:
        confirmar_solicitud_pedido(order_id)
        limpiar_id_reserva(order_id)
        flash("Compra confirmada. Tu producto ya quedó comprado.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("index") + "#mis-reservas")


@app.route("/reserva/<order_id>/cancelar", methods=["POST"])
def cancelar_reserva_route(order_id):
    try:
        sales = obtener_coleccion("sales")
        order = sales.find_one({"id": order_id, "status": "pending"}, {"_id": 0})
        if order is None:
            raise ValueError("La reserva ya fue procesada o no existe.")
        sales.update_one(
            {"id": order_id, "status": "pending"},
            {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc), "cancelled_by": session.get("user_id")}},
        )
        limpiar_id_reserva(order_id)
        flash("La reserva fue cancelada y el producto quedó disponible nuevamente.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("index") + "#mis-reservas")


@app.route("/carrito/agregar", methods=["POST"])
def agregar_al_carrito():
    product_id = request.form.get("product_id", "").strip()
    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 1
    if not product_id:
        flash("Selecciona un producto válido para el carrito.", "error")
        return redirect(url_for("index") + "#postres")
    if quantity < 1:
        flash("La cantidad debe ser mayor que cero.", "error")
        return redirect(url_for("index") + "#postres")

    product = obtener_coleccion("products").find_one({"id": product_id, "active": True}, {"_id": 0, "name": 1})
    if product is None:
        flash("Este producto ya no está disponible.", "error")
        return redirect(url_for("index") + "#postres")

    stock = obtener_coleccion("inventory").find_one({"product_id": product_id}, {"_id": 0, "quantity": 1})
    if stock is None or stock.get("quantity", 0) < quantity:
        flash("No hay suficiente existencia para agregar este producto al carrito.", "error")
        return redirect(url_for("index") + "#postres")

    carrito = obtener_carrito()
    agregado = False
    for item in carrito:
        if item["product_id"] == product_id:
            item["quantity"] += quantity
            agregado = True
            break
    if not agregado:
        carrito.append({"product_id": product_id, "quantity": quantity})
    session["cart"] = carrito
    flash(f"Se agregó {product['name']} a tu carrito.", "success")
    return redirect(url_for("index") + "#postres")


@app.route("/carrito/vaciar", methods=["POST"])
def vaciar_carrito_route():
    session["cart"] = []
    flash("Carrito vaciado.", "success")
    return redirect(url_for("index") + "#postres")


@app.route("/carrito/reservar", methods=["POST"])
def reservar_carrito():
    carrito = obtener_carrito()
    if not carrito:
        flash("Tu carrito está vacío.", "error")
        return redirect(url_for("index") + "#postres")

    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip().casefold()
    note = request.form.get("note", "").strip()

    if not customer_name or len(customer_name) > 100:
        flash("Escribe tu nombre para reservar los artículos.", "error")
        return redirect(url_for("index") + "#postres")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer_email):
        flash("Escribe un correo electrónico válido para confirmar la reserva.", "error")
        return redirect(url_for("index") + "#postres")
    if len(note) > 500:
        flash("La nota no puede superar los 500 caracteres.", "error")
        return redirect(url_for("index") + "#postres")

    reserved = []
    for item in carrito:
        try:
            pedido = registrar_solicitud_pedido(
                item["product_id"],
                item["quantity"],
                customer_name,
                customer_email,
                note,
            )
            reserved.append(pedido)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("index") + "#postres")

    session["cart"] = []
    flash(f"Reservaste {len(reserved)} artículo(s). La entrega solo queda pendiente durante {RESERVATION_SECONDS} segundos.", "success")
    return redirect(url_for("index") + "#mis-reservas")


@app.route("/inicio", methods=["GET", "POST"])
def inicio():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        normalized_email = email.strip().casefold()

        if normalized_email == FALLBACK_ADMIN_EMAIL.casefold() and password == FALLBACK_ADMIN_PASSWORD:
            session.clear()
            session["user_id"] = "fallback-admin"
            return redirect(url_for("index"))

        if not email or not password:
            return render_template("inicio.html", error="Ingresa tu correo y contraseña.")

        user = obtener_coleccion("users").find_one({"email": normalized_email, "active": True})
        if not user or not check_password_hash(user["password_hash"], password):
            return render_template(
                "inicio.html",
                error="El correo o la contraseña no son correctos.",
            ), 401

        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for("index"))

    return render_template("inicio.html")


@app.route("/registro", methods=["POST"])
def registro():
    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return render_template(
            "inicio.html",
            error="Escribe un correo electrónico válido para crear tu cuenta.",
        ), 400
    if len(password) < 8:
        return render_template(
            "inicio.html",
            error="La contraseña debe tener al menos 8 caracteres.",
        ), 400

    user = {
        "id": uuid.uuid4().hex,
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": "customer",
        "active": True,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        obtener_coleccion("users").insert_one(user)
    except PyMongoError as error:
        if getattr(error, "code", None) == 11000:
            return render_template(
                "inicio.html",
                error="Ese correo ya tiene una cuenta. Inicia sesión con esa cuenta.",
            ), 409
        raise

    session.clear()
    session["user_id"] = user["id"]
    return redirect(url_for("index"))


@app.route("/administracion")
def administracion():
    requerir_admin()
    revisar_reservas_vencidas()
    inventory = list(
        obtener_coleccion("inventory").aggregate(
            [
                {
                    "$lookup": {
                        "from": "products",
                        "localField": "product_id",
                        "foreignField": "id",
                        "as": "product",
                    }
                },
                {"$unwind": "$product"},
                {
                    "$project": {
                        "_id": 0,
                        "product_id": 1,
                        "quantity": 1,
                        "product_name": "$product.name",
                        "price_cents": "$product.price_cents",
                    }
                },
                {"$sort": {"product_name": 1}},
            ]
        )
    )
    return render_template(
        "administracion.html",
        usuario_email=obtener_usuario_actual()["email"],
        productos=obtener_catalogo(),
        categorias=PRODUCT_CATEGORIES,
        inventory=inventory,
        ventas=list(obtener_coleccion("sales").find({"status": "completed"}, {"_id": 0}).sort("created_at", DESCENDING).limit(10)),
        pedidos=list(obtener_coleccion("sales").find({"status": "pending"}, {"_id": 0}).sort("created_at", DESCENDING).limit(50)),
        compras=list(obtener_coleccion("purchases").find({}, {"_id": 0}).sort("created_at", DESCENDING).limit(10)),
        users=list(obtener_coleccion("users").find({}, {"_id": 0, "password_hash": 0}).sort("created_at", DESCENDING).limit(25)),
    )


@app.route("/administracion/productos", methods=["POST"])
def agregar_producto():
    requerir_admin()

    name = request.form.get("name", "").strip()
    category = request.form.get("category", "")
    raw_price = request.form.get("price", "").strip()
    raw_quantity = request.form.get("quantity", "0").strip()
    photo = request.files.get("photo")

    if not name or len(name) > 100:
        flash("Escribe un nombre de hasta 100 caracteres.", "error")
        return redirect(url_for("administracion"))
    if category not in PRODUCT_CATEGORIES:
        flash("Selecciona una categoría válida.", "error")
        return redirect(url_for("administracion"))
    try:
        price = Decimal(raw_price)
    except InvalidOperation:
        price = Decimal("0")
    if not price.is_finite() or price <= 0 or price > Decimal("999999.99"):
        flash("Escribe un precio mayor que $0 y menor que $1,000,000.", "error")
        return redirect(url_for("administracion"))
    if price != price.quantize(Decimal("0.01")):
        flash("El precio puede tener como máximo dos decimales.", "error")
        return redirect(url_for("administracion"))
    try:
        initial_quantity = int(raw_quantity)
    except ValueError:
        initial_quantity = -1
    if initial_quantity < 0 or initial_quantity > 100000:
        flash("El stock inicial debe ser un número entero entre 0 y 100,000.", "error")
        return redirect(url_for("administracion"))
    if photo is None or not photo.filename:
        flash("Selecciona una foto para el producto.", "error")
        return redirect(url_for("administracion"))

    extension = Path(photo.filename).suffix.casefold()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        flash("La foto debe ser JPG, PNG, GIF o WEBP.", "error")
        return redirect(url_for("administracion"))

    image_header = photo.stream.read(12)
    photo.stream.seek(0)
    valid_image = (
        (extension in {".jpg", ".jpeg"} and image_header.startswith(b"\xff\xd8\xff"))
        or (extension == ".png" and image_header.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension == ".gif" and image_header[:6] in {b"GIF87a", b"GIF89a"})
        or (
            extension == ".webp"
            and image_header.startswith(b"RIFF")
            and image_header[8:12] == b"WEBP"
        )
    )
    if not valid_image:
        flash("El archivo seleccionado no parece ser una imagen válida.", "error")
        return redirect(url_for("administracion"))

    photo_data = photo.read()
    product_id = uuid.uuid4().hex
    image_name = f"{product_id}{extension}"
    product = {
        "id": product_id,
        "name": name,
        "category": category,
        "category_label": PRODUCT_CATEGORIES[category],
        "description": "Preparado con cariño en Dulce Encanto.",
        "image": image_name,
        "tag": "Nuevo",
        "price_cents": int(price * 100),
        "uploaded": True,
        "active": True,
    }
    photo_mimetype = mimetypes.guess_type(photo.filename)[0] or "application/octet-stream"
    guardar_producto(product, photo_data, photo_mimetype, initial_quantity)

    flash(f"Se agregó «{name}» al catálogo con {initial_quantity} unidades.", "success")
    return redirect(url_for("administracion"))


@app.route("/pedido", methods=["POST"])
def crear_pedido():
    try:
        quantity = int(request.form.get("quantity", "0"))
    except ValueError:
        quantity = 0
    try:
        registrar_solicitud_pedido(
            request.form.get("product_id", ""),
            quantity,
            request.form.get("customer_name", "").strip(),
            request.form.get("customer_email", "").strip().casefold(),
            request.form.get("note", "").strip(),
        )
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index") + "#postres")

    flash(
        "Recibimos tu solicitud. La pastelería confirmará disponibilidad y el total por correo.",
        "success",
    )
    return redirect(url_for("index") + "#postres")


@app.route("/administracion/compras", methods=["POST"])
def crear_compra():
    requerir_admin()
    product_id = request.form.get("product_id", "")
    supplier = request.form.get("supplier", "").strip()
    try:
        quantity = int(request.form.get("quantity", "0"))
        unit_cost = Decimal(request.form.get("unit_cost", "0"))
    except (ValueError, InvalidOperation):
        flash("Revisa la cantidad y el costo unitario.", "error")
        return redirect(url_for("administracion"))

    if quantity < 1 or quantity > 100000 or not unit_cost.is_finite() or unit_cost <= 0:
        flash("La cantidad y el costo de compra deben ser mayores que cero.", "error")
        return redirect(url_for("administracion"))
    if unit_cost > Decimal("999999.99") or unit_cost != unit_cost.quantize(Decimal("0.01")):
        flash("El costo debe tener como máximo dos decimales y no superar $999,999.99.", "error")
        return redirect(url_for("administracion"))
    if not supplier or len(supplier) > 120:
        flash("Escribe el proveedor (hasta 120 caracteres).", "error")
        return redirect(url_for("administracion"))

    try:
        compra = registrar_compra(product_id, quantity, int(unit_cost * 100), supplier)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("administracion"))

    flash(f"Compra registrada por ${compra['total_cents'] / 100:.2f}; inventario actualizado.", "success")
    return redirect(url_for("administracion"))


@app.route("/administracion/pedidos/<order_id>/confirmar", methods=["POST"])
def confirmar_pedido(order_id):
    requerir_admin()
    try:
        confirmar_solicitud_pedido(order_id)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("administracion"))

    flash("Pedido confirmado y existencias descontadas.", "success")
    return redirect(url_for("administracion"))


@app.route("/administracion/pedidos/<order_id>/cancelar", methods=["POST"])
def cancelar_pedido(order_id):
    requerir_admin()
    result = obtener_coleccion("sales").update_one(
        {"id": order_id, "status": "pending"},
        {
            "$set": {
                "status": "cancelled",
                "cancelled_at": datetime.now(timezone.utc),
                "cancelled_by": session.get("user_id"),
            }
        },
    )
    if result.modified_count != 1:
        flash("La solicitud ya fue procesada o no existe.", "error")
    else:
        flash("La solicitud fue cancelada.", "success")
    return redirect(url_for("administracion"))


@app.route("/administracion/ventas", methods=["POST"])
def crear_venta():
    requerir_admin()
    product_id = request.form.get("product_id", "")
    customer_email = request.form.get("customer_email", "").strip().casefold()
    try:
        quantity = int(request.form.get("quantity", "0"))
    except ValueError:
        quantity = 0

    if quantity < 1 or quantity > 100000:
        flash("La cantidad de la venta debe ser mayor que cero.", "error")
        return redirect(url_for("administracion"))
    if customer_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer_email):
        flash("Escribe un correo válido para el cliente o deja el campo vacío.", "error")
        return redirect(url_for("administracion"))

    try:
        venta = registrar_venta(product_id, quantity, customer_email)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("administracion"))

    flash(f"Venta registrada por ${venta['total_cents'] / 100:.2f}; inventario actualizado.", "success")
    return redirect(url_for("administracion"))


@app.route("/administracion/usuarios", methods=["POST"])
def crear_usuario():
    requerir_admin()
    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")
    role = request.form.get("role", "customer")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Escribe un correo válido para el usuario.", "error")
        return redirect(url_for("administracion"))
    if len(password) < 12:
        flash("La contraseña debe tener al menos 12 caracteres.", "error")
        return redirect(url_for("administracion"))
    if role not in {"admin", "customer"}:
        flash("Selecciona un tipo de usuario válido.", "error")
        return redirect(url_for("administracion"))

    try:
        obtener_coleccion("users").insert_one(
            {
                "id": uuid.uuid4().hex,
                "email": email,
                "password_hash": generate_password_hash(password),
                "role": role,
                "active": True,
                "created_at": datetime.now(timezone.utc),
            }
        )
    except PyMongoError as error:
        if getattr(error, "code", None) == 11000:
            flash("Ya existe una cuenta con ese correo.", "error")
            return redirect(url_for("administracion"))
        raise

    flash(f"Se creó la cuenta de {email}.", "success")
    return redirect(url_for("administracion"))


@app.route("/cerrar-sesion", methods=["POST"])
def cerrar_sesion():
    session.clear()
    return redirect(url_for("index"))


@app.route("/img/<path:filename>")
def servir_imagen(filename):
    return send_from_directory(
        os.path.join(app.root_path, "img"),
        filename,
        max_age=86400,
    )


@app.route("/product-images/<path:filename>")
def product_image(filename):
    product = obtener_coleccion_productos().find_one(
        {"id": filename},
        {"_id": 0, "photo": 1, "photo_mimetype": 1},
    )
    if product is None or "photo" not in product:
        abort(404)
    response = send_file(
        BytesIO(product["photo"]),
        mimetype=product["photo_mimetype"],
        download_name=filename,
        max_age=31536000,
        conditional=True,
    )
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.cli.command("init-db")
def init_db_command():
    """Create MongoDB collections, indexes and the initial catalog."""
    inicializar_base_datos()
    click.echo(f"MongoDB conectado. Base '{MONGODB_DATABASE}' lista.")
    click.echo("Colecciones: " + ", ".join(COLLECTION_NAMES))


@app.cli.command("create-admin")
@click.option("--email", prompt=True, help="Correo de la cuenta administradora.")
def create_admin_command(email):
    """Create an administrator account in MongoDB."""
    inicializar_base_datos()
    normalized_email = email.strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
        raise click.ClickException("Escribe un correo válido.")
    password = click.prompt("Contraseña (mínimo 12 caracteres)", hide_input=True, confirmation_prompt=True)
    if len(password) < 12:
        raise click.ClickException("La contraseña debe tener al menos 12 caracteres.")
    try:
        obtener_coleccion("users").insert_one(
            {
                "id": uuid.uuid4().hex,
                "email": normalized_email,
                "password_hash": generate_password_hash(password),
                "role": "admin",
                "active": True,
                "created_at": datetime.now(timezone.utc),
            }
        )
    except PyMongoError as error:
        if getattr(error, "code", None) == 11000:
            raise click.ClickException("Ya existe una cuenta con ese correo.") from error
        raise
    click.echo(f"Cuenta administradora creada: {normalized_email}")


@app.cli.command("reset-admin")
def reset_admin_command():
    """Change the email and password of an existing administrator account."""
    inicializar_base_datos()
    users = obtener_coleccion("users")
    admins = list(
        users.find(
            {"role": "admin"},
            {"_id": 0, "id": 1, "email": 1, "active": 1},
        ).sort("email", ASCENDING)
    )
    if not admins:
        raise click.ClickException(
            "No hay cuentas administradoras. Crea una con create-user --role admin."
        )

    click.echo("Cuentas administradoras:")
    for index, admin in enumerate(admins, start=1):
        status = "activa" if admin.get("active") else "inactiva"
        click.echo(f"{index}. {admin['email']} ({status})")

    selection = click.prompt(
        "Número de la cuenta que quieres actualizar",
        type=click.IntRange(1, len(admins)),
    )
    admin = admins[selection - 1]
    new_email = click.prompt("Nuevo correo").strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new_email):
        raise click.ClickException("Escribe un correo válido.")

    password = click.prompt(
        "Nueva contraseña (mínimo 12 caracteres)",
        hide_input=True,
        confirmation_prompt=True,
    )
    if len(password) < 12:
        raise click.ClickException("La contraseña debe tener al menos 12 caracteres.")

    try:
        result = users.update_one(
            {"id": admin["id"], "role": "admin"},
            {
                "$set": {
                    "email": new_email,
                    "password_hash": generate_password_hash(password),
                }
            },
        )
    except PyMongoError as error:
        if getattr(error, "code", None) == 11000:
            raise click.ClickException("Ya existe una cuenta con ese correo.") from error
        raise
    if result.matched_count != 1:
        raise click.ClickException("La cuenta seleccionada ya no existe o no es administradora.")
    click.echo(f"Credenciales actualizadas para: {new_email}")


@app.cli.command("create-user")
@click.option("--email", prompt=True, help="Correo de la cuenta.")
@click.option("--role", type=click.Choice(["admin", "customer"]), default="customer", prompt=True)
def create_user_command(email, role):
    """Create a user account in MongoDB."""
    inicializar_base_datos()
    normalized_email = email.strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
        raise click.ClickException("Escribe un correo válido.")
    password = click.prompt("Contraseña (mínimo 12 caracteres)", hide_input=True, confirmation_prompt=True)
    if len(password) < 12:
        raise click.ClickException("La contraseña debe tener al menos 12 caracteres.")
    try:
        obtener_coleccion("users").insert_one(
            {
                "id": uuid.uuid4().hex,
                "email": normalized_email,
                "password_hash": generate_password_hash(password),
                "role": role,
                "active": True,
                "created_at": datetime.now(timezone.utc),
            }
        )
    except PyMongoError as error:
        if getattr(error, "code", None) == 11000:
            raise click.ClickException("Ya existe una cuenta con ese correo.") from error
        raise
    click.echo(f"Cuenta {role} creada: {normalized_email}")


@app.cli.command("migrate-products-to-mongodb")
def migrate_products_to_mongodb():
    """Move locally saved products and their photos into MongoDB."""
    if not PRODUCTS_FILE.exists():
        click.echo("No hay productos locales para migrar.")
        return

    with PRODUCTS_FILE.open(encoding="utf-8") as products_file:
        products = json.load(products_file)
    collection = obtener_coleccion_productos()
    inventory_collection = obtener_coleccion("inventory")
    migrated = 0
    for product in products:
        image_path = PRODUCT_IMAGES_DIRECTORY / product["image"]
        if not image_path.is_file():
            raise click.ClickException(f"No se encontró la foto local: {image_path.name}")
        photo_data = image_path.read_bytes()
        product_id = product.get("id") or uuid.uuid4().hex
        collection.update_one(
            {"id": product_id},
            {
                "$setOnInsert": {
                    **product,
                    "id": product_id,
                    "image": product_id,
                    "uploaded": True,
                    "active": True,
                    "photo": photo_data,
                    "photo_mimetype": mimetypes.guess_type(image_path.name)[0]
                    or "application/octet-stream",
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        inventory_collection.update_one(
            {"product_id": product_id},
            {"$setOnInsert": {"product_id": product_id, "quantity": 0, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        migrated += 1
    click.echo(f"Migración terminada: {migrated} productos procesados.")


# =========================
# INICIAR SERVIDOR
# =========================

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "").casefold() == "true")
