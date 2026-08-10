# Transcripcion y calidad de audio

Usa esta guia cuando el texto sale mal, faltan palabras, hay errores en nombres, o falla ElevenLabs Speech to Text.

## ElevenLabs es requerido

### Que significa

Contenido Bionico necesita una clave API de ElevenLabs con permiso `speech_to_text`. El corte depende de una transcripcion palabra por palabra con timestamps precisos. Sin ElevenLabs, la calidad del corte baja drasticamente porque una transcripcion local puede perder retakes, palabras cortadas y pausas importantes.

### Solucion

Crea una cuenta gratis en ElevenLabs, genera una clave API con permiso `speech_to_text` y guardala en `.env` como `ELEVENLABS_API_KEY`.

La cuenta gratis incluye creditos mensuales. Como referencia, 3 minutos de transcripcion suelen consumir unos 60 creditos, asi que las primeras pruebas normalmente entran dentro de los creditos gratis si la cuenta tiene creditos disponibles.

### Walkthrough

La guia paso a paso vivira en la comunidad: https://www.skool.com/bionico

## Error: ElevenLabs dice clave invalida o sin permiso speech_to_text

### Que significa

La clave puede estar mal copiada, vencida o creada sin el permiso de Speech to Text.

### Solucion

Genera una nueva clave API en ElevenLabs y asegurate de activar el permiso `speech_to_text`. Despues guarda la clave en `.env` como `ELEVENLABS_API_KEY`.

### Walkthrough

Si quieres ver como crear la clave correcta, usa la guia de la comunidad: https://www.skool.com/bionico

## Error: ElevenLabs dice sin creditos o error de red

### Que significa

La cuenta puede no tener creditos disponibles, o la conexion fallo durante la llamada.

### Solucion

Revisa los creditos de tu cuenta de ElevenLabs. Si todavia tienes creditos, intenta de nuevo. Si no tienes creditos, espera al reinicio mensual de la cuenta gratis o agrega creditos.

### Walkthrough

Si quieres ver como revisar creditos y errores comunes de ElevenLabs, usa la guia de la comunidad: https://www.skool.com/bionico

## Error: faltan palabras o el transcript queda cortado

### Que significa

La transcripcion no cubrio todo el audio o el proveedor devolvio un resultado incompleto. Puede pasar con audios largos, mala conexion, ruido, silencios raros o cortes internos.

### Solucion

Vuelve a correr el video con la version mas reciente. Si sigue pasando, prueba con mejor audio. Si el problema ocurre siempre en el mismo segundo, reporta el segundo exacto y conserva los archivos del run.

### Walkthrough

Si quieres ver como diagnosticar una transcripcion incompleta, hay un video gratuito en la comunidad: https://www.skool.com/bionico

## Error: nombres, marcas o palabras raras salen mal

### Que significa

Los modelos de transcripcion pueden confundir nombres propios, marcas, palabras en ingles, dominios, siglas o frases muy rapidas.

### Solucion

Usa audio claro y habla un poco mas separado en nombres importantes. Si el error afecta una palabra importante, reporta el texto correcto y el segundo del video.

### Walkthrough

Si quieres ver como mejorar nombres, marcas y palabras tecnicas en la transcripcion, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: el audio del video no se puede leer

### Que significa

El archivo puede no tener pista de audio, estar corrupto, usar un codec raro o no ser realmente un video compatible.

### Solucion

Prueba reproducir el video localmente. Si no tiene audio, exportalo de nuevo con audio. Si el archivo viene de una app rara, reexportalo como `.mp4` con audio normal.

### Walkthrough

Si quieres ver como revisar si un video tiene audio util antes de procesarlo, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico
