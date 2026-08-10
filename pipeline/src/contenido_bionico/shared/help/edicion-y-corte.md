# Edicion, corte y timing

Usa esta guia cuando el corte final no coincide con el texto aprobado, hay frases mal cortadas, queda demasiado silencio, o el video cortado tiene errores.

## Error: el texto aprobado esta bien, pero el video cortado dice otra cosa

### Que significa

El editor/reviewer aprobaron una version del texto, pero el mapeo entre ese texto y los timestamps de la transcripcion fallo. Esto puede pasar cuando hay palabras duplicadas, palabras con duracion cero, puntuacion rara, o transcripcion con poca precision.

### Solucion

Actualiza a la version mas reciente y vuelve a procesar. Si sigue pasando, reporta el segundo exacto, la frase esperada, la frase que aparece en el video, y conserva `final.txt`, `transcript.json`, el MP4 cortado de salida y los archivos de EDL del run.

### Walkthrough

Si quieres ver como comparar texto aprobado contra video cortado, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: se corto una palabra o frase que si querias conservar

### Que significa

El sistema intenta eliminar repeticiones, muletillas y reformulaciones. Si el transcript o el editor interpretan mal una repeticion intencional, puede cortar de mas.

### Solucion

Revisa el texto aprobado por el editor/reviewer. Si la frase no esta ahi, el problema fue editorial. Si si esta ahi pero no aparece en video, el problema fue de mapeo/timing y conviene reportarlo como bug.

### Walkthrough

Si quieres ver como distinguir un error editorial de un error de timing, hay un video gratuito en la comunidad: https://www.skool.com/bionico

## Error: queda demasiado silencio

### Que significa

El sistema deja margenes para que el corte no se sienta roto. Si el audio original tiene pausas largas o respiraciones, puede quedar mas silencio del deseado.

### Solucion

Primero confirma que el resultado no sea intencional por claridad. Si se siente lento, reporta el segundo exacto y conserva el run. No edites manualmente los archivos internos si quieres que el problema sea reproducible.

### Walkthrough

Si quieres ver como evaluar si un silencio debe quedarse o cortarse, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: el corte se siente muy agresivo o salta raro

### Que significa

Puede haber cortes demasiado cerca de palabras, respiraciones o micro-silencios. Tambien puede pasar si el video original tiene audio desincronizado o muchos retakes.

### Solucion

Usa un video de entrada con audio limpio y evita hablar encima de cortes o ruidos. Si el salto ocurre siempre en el mismo lugar, reporta ese timestamp.

### Walkthrough

Si quieres ver como grabar y revisar material para que los cortes salgan limpios, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: el video cortado falta o no se puede leer

### Que significa

La fase de corte no produjo el video cortado, no lo pudo mover a la carpeta de salida, o ffmpeg/ffprobe no puede leer el archivo.

### Solucion

Revisa la carpeta de salida `output_short`. En un run de solo corte debe existir `contenido-bionico-<run_id>-cortado.mp4`. Si ffmpeg o ffprobe faltan del PATH, arregla eso primero. Si el archivo original esta corrupto, reexportalo y vuelve a procesar.

### Walkthrough

Si quieres ver como revisar el video cortado y confirmar si el problema es del archivo o de la herramienta, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico
