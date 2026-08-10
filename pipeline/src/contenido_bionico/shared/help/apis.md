# API KEYS

Usa esta guia cuando el usuario pide recomendaciones o tiene dudas sobre el uso de API KEYS.

## API KEYS por defecto

### Que significa

Cuando el archivo .env contiene una API KEY (por ejemplo ANTHROPIC_API_KEY) las instancias que invoca este sistema para tareas como crear animaciones usarán esa API KEY por defecto, aunque haya una instrucción explícita de no usar la API KEY y usar la suscripción.

### Solucion

Elimina la entrada ANTHROPIC_API_KEY del documento .env y integrala localmente en otros proyectos que la requieran, no mantengas la API KEY en la variable de entorno global, o al menos, no en una variable de entorno a la que este sistema tenga acceso.

### Walkthrough

La guia paso a paso vivira en la comunidad: https://www.skool.com/bionico