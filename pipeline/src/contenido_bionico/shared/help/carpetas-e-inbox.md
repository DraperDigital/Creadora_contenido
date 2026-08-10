# Carpetas, inbox y rutas de video

Usa esta guia cuando el usuario no sepa donde poner videos, el comando diga que no encuentra el archivo, o el inbox no procese nada.

## Error: `video not found`

### Que significa

La ruta que se paso al sistema no existe, esta mal escrita, tiene comillas mal puestas, o apunta a otra carpeta.

### Solucion

Pide la ruta completa del archivo de video. En Windows puede verse asi:

```text
%USERPROFILE%\Videos\mi-video.mp4
```

Si la ruta tiene espacios, mantenla completa y entre comillas cuando se ejecute internamente.

### Walkthrough

Si quieres ver como copiar la ruta correcta de un video en Windows, hay un video gratuito en la comunidad: https://www.skool.com/bionico

## Error: el inbox esta vacio o el formato no se acepta

### Que significa

El usuario quiere procesar desde la carpeta de entrada, pero no hay ningun archivo compatible dentro de esa carpeta, o el archivo esta en un formato que no se acepta. El inbox acepta `.mp4`, `.mov` o `.m4v`.

Los `.webm` y `.mkv` no se aceptan: el comando los rechaza con un mensaje claro pidiendo convertir el archivo a MP4 (H.264).

### Solucion

Confirma cual es la carpeta de entrada configurada. Luego pide al usuario que ponga ahi un archivo en un formato aceptado. Si su archivo es `.webm` o `.mkv`, conviertelo a MP4 (H.264) primero.

Despues procesa el video pasando su ruta completa (agrega las banderas del modo que corresponda):

```bash
contenido-bionico "<ruta/al/video.mp4>" --short
```

### Walkthrough

Si quieres ver el flujo de dejar videos en una carpeta de entrada y procesarlos desde ahi, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: estas probando en la carpeta equivocada

### Que significa

Puede haber una carpeta de desarrollo, una carpeta de prueba y una copia archivada. Si el asistente instala o ejecuta desde otra carpeta, los resultados no prueban la version que querias revisar.

### Solucion

Antes de probar, confirma la carpeta exacta. Para pruebas limpias, usa una copia separada como:

```text
contenido-bionico-test
```

No mezcles resultados de prueba con el repo fuente.

### Walkthrough

Si quieres ver como hacer una prueba limpia sin contaminar tu instalacion principal, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: un run no avanza o no sabes en que va

### Que significa

No hay ningun watcher de carpetas: cada video se procesa con un comando directo, y el avance queda registrado en el run. Si parece que nada avanza, puede ser una fase larga (las animaciones tardan 10-20 minutos por escena) o un fallo real.

### Solucion

No dupliques el video ni lo renombres varias veces. Revisa el estado con `contenido-bionico status <run_id>` (o sin run_id para ver el mas reciente). Si el heartbeat esta fresco, espera. Si el run fallo, reanudalo con `contenido-bionico --animate-run <run_id>`: los segmentos ya completados se reutilizan. No vuelvas a ejecutar el comando sobre el archivo original: eso crea un run nuevo desde cero.

### Walkthrough

Si quieres ver como leer el estado de un video en proceso sin romper la corrida, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico
