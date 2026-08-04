# EF5 — Workshop para Guatemala (Día 1)

Configuración independiente de EF5 para Guatemala (900 m) que ejecuta el modelo hidrológico mediante un contenedor de Docker, mientras **TITO_GuatemalaTraining permanece completamente intacto**. La imagen de EF5 es la misma (`ef5-container`) utilizada por TITO; únicamente cambia la organización de los archivos para la ejecución.

---

## Estructura de carpetas

```text
EF5_GuatemalaTraining/
├── data/                        → montada como /data (entradas del modelo, lectura/escritura)
│   ├── basic/                   DEM, dirección de flujo (DDM), acumulación de flujo (FAM)
│   ├── parameters/              Grillas CREST_Guatemala_900m/ y KW_Guatemala_900m/
│   ├── pet/                     Evapotranspiracion potencial PET.01.tif … PET.12.tif
│   ├── states/                  Estados del modelo para inicio (crest_SM, kwr_*)
│   └── precip/
│       └── imerg/               Forzamiento IMERG: imerg.qpe.YYYYMMDDHHUU.30minAccum.tif
├── output/                      → montada como /output (resultados de EF5)
├── conf/                        → montada como /conf (archivos de configuración, solo lectura)
│   ├── control.txt              Archivo de control de EF5 para Guatemala a 900 m
│   └── Guatemala_900m_basin_new.txt
│                                 Definición de estaciones/cuencas (incluida en control.txt)
├── docker/
│   ├── Dockerfile               Construye ef5-container:latest desde el código fuente (AHWALab/EF5)
│   ├── build_ef5.sh             Construcción/reutilización (Linux y macOS)
│   ├── build_ef5.ps1            Construcción/reutilización (Windows PowerShell)
│   └── ef5-container.tar        Archivo de imagen precompilada (uso sin conexión)
├── docker-compose.yml           Lanzador multiplataforma (funciona en Linux, macOS y Windows)
├── run_ef5.sh                   Ejecuta EF5 (Linux / macOS / WSL)
├── run_ef5.ps1                  Ejecuta EF5 (Windows PowerShell)
├── README_english.md            Versión en inglés de README
└── README.md
```

---

## Cómo accede el contenedor a las carpetas

Los scripts `run_ef5.sh`, `run_ef5.ps1` y `docker-compose.yml` montan las tres carpetas principales dentro del contenedor y ejecutan EF5 desde la raíz del mismo. De esta forma, todas las rutas definidas en el archivo de control son relativas a `/`.

| Carpeta en el host | Ruta en el contenedor | Uso |
|--------------------|-----------------------|-----|
| `./data` | `/data` | Datos de entrada: basic, parameters, pet, states y precip |
| `./output` | `/output` | Resultados de EF5 (maxq, maxunitq, `ts.*.tif`, registros y archivos CSV) |
| `./conf` | `/conf` | Archivo(s) de control de EF5 |

---

## Construir o reutilizar la imagen

### Linux / macOS

El script `docker/build_ef5.sh` **no recompila la imagen a menos que se solicite explícitamente**.

```bash
./docker/build_ef5.sh                 # reutiliza la imagen existente / carga el archivo / compila
./docker/build_ef5.sh --status        # muestra qué imagen se utilizará
./docker/build_ef5.sh --rebuild       # compila desde el código fuente (requiere internet)
./docker/build_ef5.sh --load          # carga docker/ef5-container.tar
./docker/build_ef5.sh --save          # guarda la imagen actual en docker/ef5-container.tar
```

### Windows (PowerShell)

El script `docker\build_ef5.ps1` ofrece las mismas opciones:

```powershell
.\docker\build_ef5.ps1
.\docker\build_ef5.ps1 -Status
.\docker\build_ef5.ps1 -Rebuild
.\docker\build_ef5.ps1 -Load
.\docker\build_ef5.ps1 -Save
```

El orden de reutilización es:

1. Imagen local ya cargada.
2. Archivo `docker/ef5-container.tar`.
3. Compilación desde `docker/Dockerfile` (clona el repositorio AHWALab/EF5 y compila el código; tarda unos minutos).

---

## Ejecutar EF5 — Linux, Windows y macOS

| Plataforma | Comando |
|------------|---------|
| Linux / WSL | `./run_ef5.sh` |
| macOS | `./run_ef5.sh` (usa automáticamente Docker Compose) |
| Windows (PowerShell) | `.\run_ef5.ps1` (usar una carpeta ubicada en la unidad C:) |
| Cualquier sistema operativo | `docker compose run --rm ef5` |

En macOS (Docker Desktop) no existe el modo de red del host (*host networking*), por lo que `run_ef5.sh` utiliza automáticamente `docker compose`. Los usuarios de Windows también pueden ejecutar los scripts `.sh` desde Git Bash o WSL.

El script `run_ef5.ps1` acepta los mismos argumentos:

```powershell
.\run_ef5.ps1                          # ejecuta conf/control.txt
.\run_ef5.ps1 -Control my_control.txt  # ejecuta un archivo de control específico (debe estar en conf/)
.\run_ef5.ps1 -Bash                    # abre una consola interactiva dentro del contenedor
```

---

## Resultados

Los resultados se almacenan en la carpeta `./output/`, incluyendo:

- Grillas `maxq`
- Grillas `maxunitq`
- Grillas de precipitación acumulada (`precipaccum`)
- Series de tiempo (`ts.*.csv`)

Antes de ejecutar una simulación con precipitación, coloque los archivos GeoTIFF de IMERG en:

```text
data/precip/imerg/
```

Si falta algún archivo de precipitación, EF5 lo tratará como si la precipitación fuera **cero**.
