# EF5 — Workshop para Guatemala (Día 1)

Configuración independiente de EF5 para Guatemala (**90 m** y **900 m**) que
ejecuta el modelo hidrológico mediante un contenedor de Docker, mientras
**TITO_GuatemalaTraining permanece completamente intacto**. La imagen de EF5 es
la misma (`ef5-container`) utilizada por TITO; únicamente cambia la organización
de los archivos para la ejecución.

---

## Estructura de carpetas

```text
EF5_GuatemalaTraining/
├── data/                        → montada como /data (entradas del modelo, lectura/escritura)
│   ├── basic/                   DEM, dirección de flujo (DDM), acumulación de flujo (FAM)
│   │   ├── DEM_guatemala_90m.tif / FDIR_guatemala_90m.tif / FAC_guatemala_90m.tif
│   │   └── DEM_guatemala_900m.tif / FDIR_guatemala_900m.tif / FAC_guatemala_900m.tif
│   ├── parameters/
│   │   ├── CREST_Guatemala_90m/   KW_Guatemala_90m/
│   │   └── CREST_Guatemala_900m/  KW_Guatemala_900m/
│   ├── pet/                     Evapotranspiración potencial PET.01.tif … PET.12.tif
│   ├── states/
│   │   ├── 90m/                 Estados de arranque 90 m (crest_SM, kwr_*)
│   │   └── 900m/                Estados de arranque 900 m (crest_SM, kwr_*)
│   └── precip/                  Forzamiento IMERG: imerg.qpe.YYYYMMDDHHUU.30minAccum.tif
├── output/                      → montada como /output (resultados de EF5)
│   ├── 90m/                     Resultados de la corrida 90 m
│   └── 900m/                    Resultados de la corrida 900 m
├── conf/                        → montada como /conf (archivos de control, solo lectura)
│   ├── control_90m.txt          Archivo de control EF5 — Guatemala 90 m
│   ├── control_900m.txt         Archivo de control EF5 — Guatemala 900 m
│   ├── Guatemala_90m_basin_new.txt   Estaciones/cuencas 90 m (incluidas en control_90m.txt)
│   └── Guatemala_900m_basin_new.txt  Estaciones/cuencas 900 m (incluidas en control_900m.txt)
├── docker/
│   ├── Dockerfile               Construye ef5-container:latest desde el código fuente (AHWALab/EF5)
│   ├── build_ef5.sh             Construcción/reutilización (Linux y macOS)
│   ├── build_ef5.ps1            Construcción/reutilización (Windows PowerShell)
│   └── ef5-container.tar        Archivo de imagen precompilada (Git LFS / uso sin conexión)
├── docker-compose.yml           Lanzador multiplataforma (Linux, macOS y Windows)
├── run_ef5.sh                   Ejecuta EF5 (Linux / macOS / WSL)
├── run_ef5.ps1                  Ejecuta EF5 (Windows PowerShell)
├── README_english.md            Versión en inglés
└── README.md
```

---

## Cómo accede el contenedor a las carpetas

Los scripts `run_ef5.sh`, `run_ef5.ps1` y `docker-compose.yml` montan las tres
carpetas principales dentro del contenedor y ejecutan EF5 desde la raíz del
mismo. De esta forma, todas las rutas definidas en el archivo de control son
relativas a `/`.

| Carpeta en el host | Ruta en el contenedor | Uso |
|--------------------|-----------------------|-----|
| `./data` | `/data` | Datos de entrada: basic, parameters, pet, states y precip |
| `./output` | `/output` | Resultados de EF5 (maxq, maxunitq, `ts.*.tif`, registros y CSV) |
| `./conf` | `/conf` | Archivos de control de EF5 (90 m y 900 m) |

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
2. Archivo `docker/ef5-container.tar` (requiere `git lfs pull` si se clonó desde GitHub).
3. Compilación desde `docker/Dockerfile` (clona AHWALab/EF5 y compila; tarda unos minutos).

---

## Ejecutar EF5 — Linux, Windows y macOS

Hay **dos archivos de control** en `conf/`. Se elige la resolución pasando la
ruta del control al lanzador:

```bash
# Linux / macOS / WSL
./run_ef5.sh conf/control_900m.txt    # Guatemala 900 m
./run_ef5.sh conf/control_90m.txt     # Guatemala 90 m
./run_ef5.sh --bash                   # consola interactiva en el contenedor
```

```powershell
# Windows PowerShell (preferible en unidad C: local — no en unidades de red mapeadas)
.\run_ef5.ps1 -Control control_900m.txt   # Guatemala 900 m
.\run_ef5.ps1 -Control control_90m.txt    # Guatemala 90 m
.\run_ef5.ps1 -Bash                       # consola interactiva
```

| Plataforma | Comando |
|------------|---------|
| Linux / WSL | `./run_ef5.sh conf/control_900m.txt` o `conf/control_90m.txt` |
| macOS | igual (`run_ef5.sh` usa Docker Compose automáticamente) |
| Windows (PowerShell) | `.\run_ef5.ps1 -Control control_900m.txt` o `control_90m.txt` |
| Cualquier SO | `docker compose run --rm ef5 /ef5/bin/ef5 /conf/control_900m.txt` |

Si no se pasa un archivo de control, el valor por defecto es
`conf/control_900m.txt`.

En macOS (Docker Desktop) no existe el modo de red del host (*host networking*),
por lo que `run_ef5.sh` utiliza automáticamente `docker compose`. Los usuarios de
Windows también pueden ejecutar los scripts `.sh` desde Git Bash o WSL.

---

## Resultados

Los resultados se escriben bajo `./output/` según la resolución del control:

| Control | Carpeta de salida | Estados |
|---------|-------------------|---------|
| `conf/control_90m.txt` | `./output/90m/` | `data/states/90m/` |
| `conf/control_900m.txt` | `./output/900m/` | `data/states/900m/` |

Incluye grillas `maxq`, `maxunitq`, precipitación acumulada y series de tiempo
(`ts.*.csv`).

Antes de ejecutar una simulación con precipitación, coloque los GeoTIFF IMERG en:

```text
data/precip/
```

con el nombre `imerg.qpe.YYYYMMDDHHUU.30minAccum.tif`. Si falta algún archivo,
EF5 lo trata como precipitación **cero**.
