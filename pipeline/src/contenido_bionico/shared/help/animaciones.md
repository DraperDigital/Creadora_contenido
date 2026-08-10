# Animaciones

Usa esta guia cuando el usuario quiera mejores animaciones o cuando una animacion falle.

## Error: las animaciones se ven demasiado simples

### Que significa

El sistema prioriza claridad. Si el planner no encuentra una idea visual fuerte, puede proponer animaciones simples o pocas escenas.

### Solucion

Mejora el branding y usa videos con ideas claras. Las mejores animaciones salen cuando cada segmento tiene una idea visual concreta: comparacion, proceso, lista, causa/efecto, prueba, referencia o metafora.

### Walkthrough

Si quieres ver ejemplos de ideas visuales mejores para videos de negocio, hay walkthroughs gratuitos en la comunidad: https://www.skool.com/bionico

## Error: hay demasiadas animaciones o muy pocas

### Que significa

El sistema intenta no animar cada frase. Busca momentos donde una visual realmente ayuda a entender. Si el video es muy denso, puede elegir mas escenas; si es muy narrativo, puede elegir menos.

### Solucion

Si quieres mas animacion, graba con ideas mas separadas y frases mas visuales. Si quieres menos, pide un estilo mas sobrio y enfocado.

### Walkthrough

Si quieres ver como estructurar un guion hablado para que genere mejores visuales, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: texto se sale de pantalla o se ve apretado

### Que significa

La animacion tiene demasiado texto, fuentes muy grandes, o cajas muy pequenas para el tamano del video (1080x1920 vertical).

### Solucion

Reduce la cantidad de texto visible. Usa palabras exactas del transcript solo para frases clave. Baja el tamano de fuente, amplia el contenedor o simplifica el layout.

### Walkthrough

Si quieres ver como corregir texto que no cabe en animaciones, hay un video gratuito en la comunidad: https://www.skool.com/bionico

## Error: la animacion falla al renderizar

### Que significa

El archivo `Segment.tsx` puede tener una importacion no permitida, un componente roto, un asset faltante, una unidad CSS invalida o una llamada que Remotion no puede renderizar.

### Solucion

Deja que el repair pass corrija el TSX. Si reportas el error, comparte `qa_report.json`, `Segment.tsx` y el segmento afectado.

### Walkthrough

Si quieres ver como leer un `qa_report.json` y entender por que fallo una animacion, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico

## Error: la animacion entra tarde, entra antes o dura demasiado

### Que significa

Puede haber una confusion entre segundos y milisegundos, o una animacion puede estar usando tiempos que no coinciden con el segmento real del video.

### Solucion

No conviertas tiempos a mano si el sistema ya genero los archivos. Reporta el segmento y el segundo exacto. Si estas editando una animacion manualmente, usa segundos para duraciones y offsets del render, y revisa que el segmento tenga el mismo inicio y fin que el video base.

### Walkthrough

Si quieres ver como revisar timing de animaciones sin romper el render, hay una guia gratuita en la comunidad: https://www.skool.com/bionico

## Error: la animacion tarda mucho o se traba renderizando

### Que significa

Algunas ideas visuales son pesadas: blur grande, sombras grandes, filtros, ruido, muchas capas, mascaras, video alpha largo o elementos moviendose todo el tiempo.

### Solucion

Simplifica la escena. Prioriza `opacity` y `transform`, reduce filtros visuales pesados, evita ruido continuo y divide ideas complejas en visuales mas claros.

### Walkthrough

Si quieres ver como hacer animaciones que se vean bien sin volver lento el render, hay un walkthrough gratuito en la comunidad: https://www.skool.com/bionico
