# Errores comunes y que reportar

Usa esta guia cuando el usuario diga "fallo", "no se que paso", "se quedo pensando", "se trabo" o "como reporto esto".

## Error: el asistente parece tardar demasiado

### Que significa

Algunas fases tardan: transcripcion, editor/reviewer, animaciones, render y reparaciones. No todo retraso es un fallo.

### Solucion

No reinicies inmediatamente. Primero identifica en que fase esta: transcripcion, corte, animacion, QA o render final. Si no hay avance por mucho tiempo, conserva los logs del run.

### Walkthrough

Si quieres ver como distinguir "esta trabajando" de "se quedo trabado", hay un video gratuito en la comunidad: https://www.skool.com/bionico

## Error: el agente no devuelve salida

### Que significa

Un proveedor de IA puede tardar, colgarse, cerrar la sesion, quedarse sin permisos o no devolver stdout correctamente.

### Solucion

Verifica que el proveedor elegido este iniciado y funcionando. Si el problema se repite, reporta el log de `runs/<id>/logs/agent-calls/`.

### Walkthrough

Si quieres ver como diagnosticar llamadas de agentes sin tocar archivos internos de mas, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: permiso denegado o inspeccion de procesos bloqueada

### Que significa

Windows puede bloquear inspeccion de procesos o acceso a ciertos detalles. Eso no siempre significa que el pipeline fallo.

### Solucion

Usa evidencia del repo: archivos generados, logs, y estados del run. No dependas solo de ver procesos internos.

### Walkthrough

Si quieres ver como diagnosticar sin depender del administrador de procesos, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: un segmento salio "degradado" o el video incluye una escena imperfecta

### Que significa

Cuando la QA de un segmento agota sus intentos, el sistema fuerza un render final. Si ese render produce un video usable, el segmento entra al video final marcado como `passed_degraded`. Por defecto esos segmentos **se incluyen**: es preferible entregar el video completo con una escena imperfecta que bloquear todo el run.

### Solucion

Si el resultado se ve bien, no hay nada que arreglar. Ten en cuenta que reanudar el run (`contenido-bionico --animate-run <id>`) NO reintenta los segmentos degradados: cuentan como completados y se reutilizan tal cual. Para rehacerlos, agrega `--force` (regenera toda la animacion desde cero) o borra los archivos de ese segmento en `runs/<id>/animations/<seg>/` antes de reanudar. La variable `BIONICO_STRICT_ANIMATIONS=1` (modo estricto) tampoco los reintenta: hace que el ensamblaje rechace los segmentos degradados y el run falle.

### Walkthrough

Si quieres ver como revisar un segmento degradado, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: un run fallo a mitad y no sabes si repetir todo

### Que significa

Los runs se pueden reanudar a nivel de segmento: los segmentos de animacion ya completados se reutilizan y solo se rehace lo que falta. Pero relanzar el mismo comando con el archivo NO reanuda nada: crea un run nuevo desde cero.

### Solucion

Reanuda el run existente con `contenido-bionico --animate-run <id>`. Usa `--force` solo si quieres regenerar todo desde cero, ignorando los planes y segmentos ya generados.

### Walkthrough

Si quieres ver como reanudar un run sin repetir trabajo, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: el reporte mezcla carpetas o versiones

### Que significa

Si hay varias copias del repo, es facil mirar logs de una copia y ejecutar otra.

### Solucion

Antes de diagnosticar, confirma la ruta exacta del repo y del run. Usa una sola carpeta por prueba.

### Walkthrough

Si quieres ver como organizar pruebas sin mezclar versiones, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: no sabes que enviar para pedir ayuda

### Que significa

Sin datos concretos, nadie puede saber si fallo transcripcion, corte, animacion o render.

### Solucion

Comparte:

- que estabas intentando hacer
- ruta del repo usado
- numero de run
- timestamp del problema en el video
- mensaje de error visible
- archivos relevantes del run, sin claves ni `.env`

### Walkthrough

Si quieres ver como pedir ayuda de forma que te puedan responder rapido, hay un video gratuito en la comunidad: https://www.skool.com/bionico
