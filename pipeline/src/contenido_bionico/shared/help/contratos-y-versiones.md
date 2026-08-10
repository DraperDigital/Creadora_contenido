# Contratos, versiones y archivos fuente

Usa esta guia cuando una instruccion parezca contradecir el repo, cuando un archivo antiguo no coincida con el formato actual, o cuando el asistente no sepa que archivo usar como fuente.

## Error: la documentacion dice que ejecutes un archivo que no existe

### Que significa

La documentacion puede estar desactualizada. En proyectos que cambian rapido, un README viejo puede mencionar comandos, carpetas o archivos que ya no son parte del flujo actual.

### Solucion

Usa primero los comandos del README principal actual. Si un documento menciona un archivo que no existe, no inventes el archivo. Busca el comando actual del CLI o pide ayuda compartiendo el nombre del documento viejo.

### Walkthrough

Si quieres ver como distinguir documentacion actual de notas historicas, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: un run viejo tiene archivos con otro formato

### Que significa

Los archivos de runs antiguos pueden tener una estructura anterior. Por ejemplo, un plan de animacion viejo puede tener campos distintos a los que usa la version actual.

### Solucion

No uses runs viejos como plantilla exacta. Para entender el formato actual, genera un run nuevo con la version instalada y compara contra los archivos nuevos.

### Walkthrough

Si quieres ver como leer artifacts de runs sin confundirte con versiones viejas, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: el asistente usa el archivo equivocado como fuente

### Que significa

Puede haber varios archivos parecidos: transcript crudo, texto aprobado, transcript ajustado, source video, request de animacion o plan creativo. Cada uno tiene una funcion distinta.

### Solucion

Usa esta regla:

- para saber que se dijo originalmente: transcript crudo
- para saber que texto fue aprobado: `final.txt` o propuesta aprobada
- para saber que video se corto: el MP4 `cortado` en la carpeta de salida
- para animar: `Animation_Request.json`
- para revisar una animacion: carpeta del segmento y `qa_report.json`

### Walkthrough

Si quieres ver como seguir la cadena de archivos de un run, hay un video gratuito en la comunidad: https://www.skool.com/bionico

## Error: el agente dice que el prompt o archivo es demasiado grande

### Que significa

Algunos archivos de pipeline son largos. Si el asistente intenta leer todo de una vez, puede quedarse sin contexto o perder detalles.

### Solucion

Divide el diagnostico por etapa: instalacion, transcripcion, corte, animacion, render final. Para animaciones, revisa primero el segmento afectado en vez de todo el run.

### Walkthrough

Si quieres ver como investigar un run grande sin perderte, hay una guia gratuita en la comunidad: https://www.skool.com/bionico
