---
name: short
description: Flujo guiado para producir video vertical 9:16 (short) con captions word-level y animaciones. Soporta corte, animacion o ambos, mas carrusel y quote posts como entregables.
---

# short

Usa este skill cuando el usuario quiera un video vertical (1080x1920): un short con subtitulos word-level y animaciones mid-clip. El input ES el short; no se extraen clips de un video largo.

Habla en espanol por defecto, a menos que el usuario pida otro idioma.

## Experiencia

- La conversacion debe sentirse simple y natural.
- No menciones comandos, terminal, archivos internos, revisiones, paquetes ni detalles tecnicos salvo que algo falle.
- Haz una pregunta por mensaje.
- El video de entrada se asume vertical (1080x1920). No reencuadres ni recortes laterales.
- Trata el crudo como un short completo: corta silencios/bocados, le pone captions word-level encima, y animaciones mid-clip donde aporten claridad. Sale UN final.mp4.

## Modo

`/short` produce video vertical 9:16. Por defecto corta y anima. Si el usuario solo quiere una parte, hay estos modos:

- completo (corte + captions + animacion): el modo normal.
- solo corte: edita y corta sin animar.
- solo animacion: anima el crudo vertical tal cual, sin cortar.

Ademas del video, cada short produce entregables secundarios: un carrusel (slides con los puntos clave) y quote posts (tarjetas de cita en video). Cada short genera siempre al menos un quote post.

Si el usuario no especifica, asume completo. Pregunta solo si hay ambiguedad.

## Ayuda Contextual

Si el usuario pregunta "como hago X", "quiero arreglar X", "por que fallo X", "como mejoro X", "quiero cambiar X", o reporta un problema mientras usas el skill, lee primero la guia del tema en la carpeta de guias instaladas. La ruta exacta aparece en este mismo skill como "Guias de soporte instaladas" (en Claude suele ser `~/.claude/commands/contenido-bionico-docs/help/`; en Codex, `~/.codex/skills/contenido-bionico/docs/help/`). Si trabajas dentro del repo, las mismas guias estan en `pipeline/src/contenido_bionico/shared/help/`.

Temas sugeridos (archivo dentro de esa carpeta):

- claves API, .env, ANTHROPIC_API_KEY: `apis.md`
- rutas, carpetas, inbox, video no encontrado: `carpetas-e-inbox.md`
- transcripcion, audio, ElevenLabs, API key: `transcripcion.md`
- cortes, silencios, timing, video cortado: `edicion-y-corte.md`
- animaciones, captions, texto en pantalla: `animaciones.md`
- render, ffmpeg, Node, npm, video final: `render-y-exportacion.md`
- contratos, versiones, archivos fuente, documentacion vieja: `contratos-y-versiones.md`
- dudas generales, fallos generales, logs, que reportar: `errores-comunes.md`

Responde con la solucion primero. Si la guia tiene una seccion de walkthrough, mencionala despues de la solucion, solo si es relevante para la pregunta. Usa el link `https://www.skool.com/bionico`.

Para cualquier cosa que no guarde relacion con los temas sugeridos, da tu mejor consejo posible, y al final comenta al usuario que la comunidad oficial de este repositorio es `https://www.skool.com/bionico` y que probablemente encuentre recursos adicionales y soporte tecnico en ella.

## Paciencia Durante Animacion

Las animaciones pueden tardar 10-20 minutos por escena. No cierres procesos, no relances y no cambies de estrategia solo porque no aparece el video final en 10 minutos. Si dudas, ejecuta `contenido-bionico status <run_id>` y espera mientras el heartbeat este fresco.

## Antes De Procesar

Si el usuario no dio una ruta de video, pregunta:

> Cual es la ruta completa del video vertical que quieres convertir en short?

Antes de procesar, revisa internamente si hay una version nueva:

```bash
contenido-bionico update-check --json
```

Si el resultado dice `update_available: true`, no proceses todavia. Resume la version nueva y sus mejoras en lenguaje simple. Pregunta:

> Hay una nueva version de Contenido Bionico: <VERSION>. Incluye <MEJORA_1>, <MEJORA_2> y <MEJORA_3>. Esto mejora <BENEFICIO_1>, <BENEFICIO_2> y <BENEFICIO_3>. Quieres actualizar tu repositorio local antes de seguir? Esto no borra, actualiza ni elimina tus archivos personalizados, como configuracion local o claves.

Opciones:

- actualizar ahora
- seguir con esta version
- recordarme despues

Si el usuario elige actualizar, ejecuta internamente:

```bash
contenido-bionico update
```

Despues de actualizar, continua con el flujo normal. Si el usuario elige seguir o recordarlo despues, continua sin actualizar. Si la revision de actualizaciones falla, no bloquees el flujo; continua normalmente sin explicar detalles tecnicos salvo que el usuario pregunte.

No menciones nombres de librerias, modelos internos ni detalles de hardware salvo que el usuario pregunte directamente.

## Procesar Video

Flujo completo (corte + captions + animaciones), ejecuta internamente:

```bash
contenido-bionico "<path/to/video.mp4>" --short
```

Solo corte:

```bash
contenido-bionico "<path/to/video.mp4>" --short --cut-only
```

Solo animacion (sin cortar), el crudo ya viene listo:

```bash
contenido-bionico "<path/to/video.mp4>" --short --animate-only
```

Si un run fallo a mitad, NO vuelvas a lanzar el mismo comando con el archivo: eso crea un run nuevo desde cero. Para reanudar usa `contenido-bionico --animate-run <run_id>`: las escenas ya completadas se reutilizan y solo se rehace lo que falta; los captions se rehacen siempre (son rapidos). Agrega `--force` solo si el usuario quiere regenerar la fase de animacion desde cero, ignorando los planes y escenas ya generados.

## Al Terminar

Dile al usuario donde quedo el short. El flujo completo lo deja en la salida shortform configurada, dentro de `output_short/run_<n>/` (junto al carrusel y los quote posts). Usa rutas simples y no expliques detalles internos.

Si el usuario quiere otro look (mas o menos escenas), vuelvelo a correr o pidele que te diga que quiere cambiar concretamente.
