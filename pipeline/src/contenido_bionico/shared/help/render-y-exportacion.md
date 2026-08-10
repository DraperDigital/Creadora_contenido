# Render, exportacion y video final

Usa esta guia cuando falla Remotion, Node, npm, ffmpeg, transparencia, o no aparece el video final.

## Error: falta Node o npm

### Que significa

Las animaciones Remotion se renderizan con herramientas de Node. Si Node o npm no estan disponibles, la fase de animacion no puede correr.

### Solucion

Instala Node.js y asegurate de que `node` y `npm` esten disponibles en el PATH. Cuando lo esten, vuelve a procesar.

### Walkthrough

Si quieres ver como instalar y verificar Node para Bionico, hay un video gratuito en la comunidad: https://www.skool.com/bionico

## La animacion tarda mucho pero no fallo

### Que significa

Crear y renderizar animaciones puede tardar 10-20 minutos por escena. Varios workers abiertos al mismo tiempo es normal.

### Solucion

No cierres procesos ni relances el pipeline solo por silencio. Revisa el estado con:

```bash
contenido-bionico status <run_id>
```

Si el heartbeat esta fresco, espera. Solo considera intervenir si `status` reporta heartbeat stale, y antes de parar algo pregunta al usuario.

## Error: falta ffmpeg o ffprobe

### Que significa

ffmpeg corta y compone el video. ffprobe revisa duraciones y archivos. Sin esas herramientas, no hay corte confiable ni render final.

### Solucion

Instala ffmpeg y agrega `ffmpeg` y `ffprobe` al PATH. Cuando esten disponibles desde la terminal, vuelve a procesar.

### Walkthrough

Si quieres ver como confirmar que ffmpeg esta bien instalado, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: la animacion pierde transparencia

### Que significa

Esto aplica a las animaciones overlay (las que se ponen encima del video con camara) y a los captions: se renderizan con alpha (`.webm`) para poder ponerse encima del video. Si el componente Remotion deja un fondo opaco durante todo el segmento, tapa el video base.

### Solucion

Mantener transparente la composicion Remotion de overlay: el fondo opaco no debe durar todo el segmento.

### Walkthrough

Si quieres ver como funciona transparencia en overlays de video, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: `final.mp4` no aparece

### Que significa

Alguna fase anterior fallo: corte, animacion, QA de segmentos o assembly final. Cuando el proceso termina bien, el MP4 final se mueve a la carpeta de salida y puede no quedarse dentro del run.

### Solucion

Revisa primero la carpeta de salida `output_short`. El flujo completo de short publica en una subcarpeta por run: `output_short/run_<n>/final_<n>.mp4` (junto al carrusel y los quote posts). Si solo cortaste, el MP4 sale como `contenido-bionico-<run_id>-cortado.mp4`. Si no aparece, conserva el run para diagnostico.

### Walkthrough

Si quieres ver como seguir la cadena `raw video -> corte temporal -> animaciones renderizadas (.webm con alpha en overlays) -> output final`, hay un video gratuito en la comunidad: https://www.skool.com/bionico

## La musica del video final no es la que quieres

### Que significa

La musica de fondo se elige sola desde `audio_library/music/shorts/` (una pista aleatoria por run). `music/no-copyright/` solo se usa como respaldo si `shorts/` esta vacio. La misma pista alimenta el video final y todos los formatos (carruseles, posters animados, quotes). La pista se repite en loop con crossfade para cubrir todo el video, asi que el punto de repeticion no se nota.

### Solucion

Para fijar una pista concreta en un run, crea `runs/<id>/Music_Override.json` con este contenido y vuelve a correr la fase de animacion del run:

```json
{"music_file": "music/shorts/mi-cancion.mp3"}
```

La ruta es relativa a `audio_library/`. Borra el archivo para volver a la seleccion aleatoria. Para cambiar el repertorio de forma permanente, agrega o quita pistas en las carpetas de `audio_library/music/`.

### Walkthrough

Si quieres ver como personalizar la musica y los SFX de tus videos, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: el MP4 final existe pero no abre o pesa 0 bytes

### Que significa

El archivo pudo haberse creado, pero el render o assembly final no termino bien. Existir no siempre significa que el video sea valido.

### Solucion

Revisa el tamano del archivo y prueba abrirlo. Si pesa 0 bytes o no se reproduce, trata el run como fallido y conserva el run, las animaciones, los logs y el MP4 roto para diagnostico.

### Walkthrough

Si quieres ver como confirmar si un archivo final es valido antes de compartirlo, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: el video final existe pero se ve raro

### Que significa

Puede haber una animacion mal posicionada, texto fuera de frame, timing desfasado, o un elemento visual mal definido.

### Solucion

Identifica el segundo exacto donde se ve raro. Luego revisa el segmento de animacion cercano a ese segundo y su `qa_report.json`.

### Walkthrough

Si quieres ver como revisar un problema visual por timestamp, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: aparecen simbolos raros en texto o logs

### Que significa

Puede haber un problema de encoding. Esto pasa cuando acentos, caracteres especiales o simbolos se guardan o leen con una codificacion distinta a UTF-8.

### Solucion

No copies texto desde logs corruptos hacia prompts o archivos finales. Vuelve a generar desde los archivos fuente si puedes. Si reportas el error, incluye el archivo donde aparece el simbolo raro y el texto correcto esperado.

### Walkthrough

Si quieres ver como detectar y reportar problemas de texto corrupto, hay un video gratuito en la comunidad: https://www.skool.com/bionico
