# Dulce Encanto

Aplicación Flask para una pastelería con MongoDB como base de datos principal.

## Requisitos e instalación

Se requiere Python 3.10+. Desde PowerShell, en la carpeta del proyecto:

```powershell
py -m pip install -r requirements.txt
```

Por defecto la aplicación se conecta a MongoDB local en `mongodb://127.0.0.1:27017/` y usa la base `dulce_encanto`. La aplicación ya admite MongoDB Atlas: configura `MONGODB_URI` y `MONGODB_DATABASE` antes de iniciar Flask. No guardes la URI ni sus credenciales en el código.

```powershell
$env:MONGODB_URI = Read-Host "Pega aquí la URI de MongoDB"
$env:MONGODB_DATABASE = "dulce_encanto"
```

### Modo sin base de datos (fallback)

Si MongoDB no está disponible en la máquina, la aplicación sigue funcionando en modo local de respaldo usando memoria del servidor. Esto permite probar la tienda, el carrito y la reserva sin que el proyecto falle. En ese modo el acceso de administrador queda habilitado con estas credenciales desde código:

- Correo: `Admin`
- Contraseña: `admin123`

La página se sigue comportando como sitio escolar y conserva la mayoría de la lógica sin depender de MongoDB.

### Conectar con MongoDB Atlas

1. Crea un clúster en MongoDB Atlas.
2. En **Database Access**, crea un usuario de base de datos y guarda su contraseña. No es la contraseña de tu cuenta de Atlas.
3. En **Network Access**, permite la IP pública del servidor donde se ejecutará la página. Evita permitir todas las IP (`0.0.0.0/0`) salvo que entiendas y aceptes ese riesgo.
4. En el clúster, elige **Connect** → **Drivers** y copia la URI `mongodb+srv://...` que ofrece Atlas. Reemplaza el usuario y la contraseña de ejemplo; si la contraseña contiene caracteres especiales, codifícala para URL.
5. En PowerShell, desde la carpeta del proyecto, pega la URI cuando la solicite el primer comando y ejecuta la inicialización:

   ```powershell
   $env:MONGODB_URI = Read-Host "Pega aquí la URI de MongoDB Atlas"
   $env:MONGODB_DATABASE = "dulce_encanto"
   flask --app app init-db
   ```

   Si todo está configurado, verás `MongoDB conectado. Base 'dulce_encanto' lista.` El comando crea las colecciones, índices y productos iniciales. Después, en esa misma ventana, crea el administrador e inicia el sitio:

   ```powershell
   flask --app app create-admin --email admin@dulceencanto.com
   python app.py
   ```

Las variables definidas con `$env:` solo duran mientras esa ventana de PowerShell siga abierta. En producción, configúralas como variables/secretos de entorno en el servicio donde despliegues la aplicación; no subas la URI a Git ni la compartas. La aplicación debe ejecutarse en un servidor y no solo en el navegador para conectarse a MongoDB.

## Preparar la base y crear el administrador

```powershell
flask --app app init-db
flask --app app create-admin --email admin@dulceencanto.com
```

El comando solicita la contraseña de forma oculta y exige al menos 12 caracteres. Después inicia el sitio:

```powershell
python app.py
```

El secreto de sesión se genera y se guarda en `instance/secret_key` si no se define `FLASK_SECRET_KEY`. La carpeta `instance` contiene secretos y archivos de desarrollo locales y no debe publicarse.

## Colecciones creadas

La inicialización crea estas colecciones e índices:

- `users`: correo único, rol y hash de contraseña.
- `products`: catálogo, precio e imagen del producto.
- `sales`: ventas, cantidades, importes y cliente opcional.
- `purchases`: compras a proveedores y sus costos.
- `inventory`: existencias actuales por producto.
- `inventory_movements`: historial de entradas por compra y salidas por venta.

Las imágenes nuevas se guardan como binarios en documentos de producto; el límite de carga es 5 MB. Los productos de muestra del catálogo se inicializan automáticamente con existencia cero. Registra una compra desde el panel para recibir unidades.

## Panel administrativo

Inicia sesión con la cuenta administradora creada por CLI. Desde el panel se pueden agregar productos, crear cuentas, registrar compras que suman al inventario, registrar ventas que descuentan existencias y consultar los últimos movimientos.

Los clientes pueden crear su propia cuenta desde la página de inicio de sesión con un correo válido y una contraseña de al menos 8 caracteres. Las cuentas creadas desde el registro tienen permisos de cliente; solo una cuenta administradora puede abrir el panel.

Para crear una cuenta de cliente por CLI:

```powershell
flask --app app create-user --email cliente@example.com --role customer
```

Las contraseñas se almacenan con hash; el acceso `admin` de demostración anterior ya no está habilitado.

Si olvidaste el correo o la contraseña de una cuenta administradora, desde PowerShell en la carpeta del proyecto ejecuta:

```powershell
flask --app app reset-admin
```

El comando muestra las cuentas administradoras, permite elegir una y solicita un correo y contraseña nuevos. No requiere las credenciales anteriores. Si usas MongoDB Atlas, configura antes las mismas variables `MONGODB_URI` y `MONGODB_DATABASE` de la aplicación.

## Migrar productos JSON antiguos

Si aún existen `instance/products.json` y sus fotos en `instance/product_images/`, cópialos a MongoDB con:

```powershell
flask --app app migrate-products-to-mongodb
```

El comando es repetible y conserva los archivos originales como respaldo. Verifica el catálogo antes de borrar cualquier copia manualmente.

## Desarrollo

```powershell
$env:FLASK_DEBUG = "true"
python app.py
```

El modo debug está desactivado por defecto. No expongas el servidor de desarrollo directamente a Internet.
