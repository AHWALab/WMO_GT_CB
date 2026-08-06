# EF5 — Workshop para Guatemala (Día 1)

Configuración independiente de EF5 para Guatemala (**90 m** y **900 m**) que
ejecuta el modelo hidrológico mediante un contenedor de Docker, mientras
**TITO_GuatemalaTraining permanece completamente intacto**. La imagen de EF5 es
la misma (`ef5-container`) utilizada por TITO; únicamente cambia la organización
de los archivos para la ejecución.

Hay archivos de control de **dominio completo** y de **cuenca (estaciones)**.
Los controles de cuenca habilitan series de tiempo en estaciones con nombre
(p. ej. Villalobos / Motagua).

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
│   ├── 900m/                    resultados — corrida de dominio completo 900 m
│   ├── 900m_cuenca/             resultados — 900 m con estaciones/cuenca
│   └── 90m_cuenca/              resultados — 90 m con estaciones/cuenca
├── conf/                        → montada como /conf (archivos de control, solo lectura)
│   ├── control_900m.txt         control de dominio completo — Guatemala 900 m
│   ├── control_900m_cuenca.txt  control con estaciones/cuenca — Guatemala 900 m
│   ├── control_90m_cuenca.txt   control con estaciones/cuenca — Guatemala 90 m
│   └── basin_list/
│       ├── Guatemala_900m_basin_new.txt
│       └── Guatemala_90m_basin_new.txt
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
| `./conf` | `/conf` | Archivos de control de EF5 |

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

## Ejecutar EF5 — elegir un archivo de control

Pase la ruta del control (debe estar bajo `conf/`):

```bash
# Linux / macOS / WSL
./run_ef5.sh conf/control_900m.txt          # dominio completo, 900 m  → output/900m/
./run_ef5.sh conf/control_900m_cuenca.txt   # estaciones/cuenca, 900 m → output/900m_cuenca/
./run_ef5.sh conf/control_90m_cuenca.txt    # estaciones/cuenca, 90 m  → output/90m_cuenca/
./run_ef5.sh --bash                         # consola interactiva en el contenedor
```

```powershell
# Windows PowerShell (preferible en unidad C: local — no en unidades de red mapeadas)
.\run_ef5.ps1 -Control control_900m.txt
.\run_ef5.ps1 -Control control_900m_cuenca.txt
.\run_ef5.ps1 -Control control_90m_cuenca.txt
.\run_ef5.ps1 -Bash
```

| Plataforma | Ejemplo |
|------------|---------|
| Linux / WSL / macOS | `./run_ef5.sh conf/control_90m_cuenca.txt` |
| Windows (PowerShell) | `.\run_ef5.ps1 -Control control_90m_cuenca.txt` |
| Cualquier SO | `docker compose run --rm ef5 /ef5/bin/ef5 /conf/control_900m.txt` |

Si no se pasa un archivo de control, el valor por defecto es
`conf/control_900m.txt`.

En macOS (Docker Desktop) no existe el modo de red del host (*host networking*),
por lo que `run_ef5.sh` utiliza automáticamente `docker compose`. Los usuarios de
Windows también pueden ejecutar los scripts `.sh` desde Git Bash o WSL.

---

## Archivos de control y resultados

| Control | Resolución | Propósito | Carpeta de salida | Estados |
|---------|------------|-----------|-------------------|---------|
| `conf/control_900m.txt` | 900 m | dominio completo | `./output/900m/` | `data/states/900m/` |
| `conf/control_900m_cuenca.txt` | 900 m | estaciones / cuenca | `./output/900m_cuenca/` | `data/states/900m/` |
| `conf/control_90m_cuenca.txt` | 90 m | estaciones / cuenca (p. ej. Villalobos) | `./output/90m_cuenca/` | `data/states/90m/` |

Las listas de cuencas/estaciones de referencia (ya incluidas en los controles
cuando corresponde) están en `conf/basin_list/`.

Los controles de cuenca marcan `outputts=true` en las estaciones seleccionadas
para que EF5 escriba series de tiempo CSV en la carpeta `output/*_cuenca/`
correspondiente.

Incluye grillas `maxq`, `maxunitq`, precipitación acumulada (y humedad del suelo
cuando está habilitada) y series `ts.*.csv` para estaciones con `outputts=true`.

Antes de ejecutar una simulación con precipitación, coloque los GeoTIFF IMERG en:

```text
data/precip/
```

con el nombre `imerg.qpe.YYYYMMDDHHUU.30minAccum.tif`. Si falta algún archivo,
EF5 lo trata como precipitación **cero**.
