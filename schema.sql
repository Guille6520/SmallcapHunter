-- SmallCap Hunter — Schema de base de datos
-- Guille, KeepCoding IA Fullstack 2026
--
-- Este schema tiene que aguantar dos modos de uso muy distintos.
-- El primero es el backfill histórico (2010-2023), que carga miles de
-- empresas de golpe. El segundo es el scheduler diario, que inserta
-- unas pocas filas cada 48h. Las decisiones de diseño que voy a tomar
-- tienen eso en cuenta.
--
-- Para crear la base de datos desde cero:
--   createdb smallcap_hunter
--   psql smallcap_hunter < schema.sql
--
-- Cambios respecto a la primera versión:
--   - Eliminé datos_mercado como tabla separada (era un join innecesario)
--   - Quité columnas calculadas de metricas_trimestrales (burn_rate, growths)
--   - Eliminé la referencia redundante a empresas en backtests
--   - Comentarios en primera persona singular
--
-- Cambios de esta versión:
--   - Eliminé el trigger de baja automática: era redundante con el
--     marcado explícito que ya hace enriquecedor_xbrl.py, disparaba un
--     select max() por cada fila insertada (58.000 veces en un backfill),
--     y podía marcar como inactiva una empresa viva si el backfill
--     insertaba sus trimestres antiguos antes que los recientes — y no
--     existía ningún camino de reactivación.
--   - Añadí check a auditorias.veredicto (antes cualquier string colaba)
--   - score_minimo_llm fijado en 25 sobre 40 (decisión final: prefiero
--     que entren más candidatas al análisis LLM y que el embudo fino lo
--     hagan los agentes — no quiero perder una explosiva por 2 puntos
--     de un scoring con pesos puestos a mano)


create extension if not exists vector;


-- Empresa es la tabla central. Todo lo demás la referencia.
-- Decidí meter aquí los datos de mercado del último snapshot porque
-- en la práctica siempre los consulto junto con la empresa — no tiene
-- sentido forzar un join para algo que necesito constantemente.
create table empresas (
    id      serial primary key,

    -- El CIK es el identificador único de la SEC. Siempre tiene 10 dígitos
    -- con ceros a la izquierda (ej: 0000320193 para Apple).
    -- Es la clave que uso para cruzar datos con XBRL y los Form 4.
    cik     varchar(10) not null unique,

    ticker  varchar(10) not null,
    nombre  varchar(255) not null,

    -- SIC es el código de sector que asigna la SEC. Con esto decido
    -- qué ventana temporal aplicar: 6Q para SaaS, 12Q para biotech.
    sic     varchar(4),
    sector  varchar(100),

    -- Snapshot de mercado en el momento del último análisis.
    -- Lo meto aquí directamente porque lo necesito en casi todas las
    -- consultas y no merece la pena una tabla separada.
    market_cap_usd      bigint,
    precio_actual       decimal(12, 4),
    precio_min_52w      decimal(12, 4),
    precio_max_52w      decimal(12, 4),

    -- Posición en el rango de 52 semanas: 0 = mínimos, 1 = máximos.
    -- Es el predictor dominante según la investigación académica.
    -- Lo precalculo en Python con: (precio - min) / (max - min)
    posicion_52w        decimal(5, 4),

    -- El volumen no lo uso como filtro de descarte (las empresas sin
    -- descubrir tienen volumen bajo por definición), pero lo guardo
    -- para que el agente red team sepa si debe reforzar el análisis
    -- de posible manipulación.
    volumen_medio_30d   bigint,

    -- Short interest (de yfinance, misma pasada que el resto del
    -- snapshot). Un cluster de insiders comprando CONTRA un short
    -- interest alto es una señal mucho más violenta que uno sin
    -- oposición — alguien va a estar muy equivocado, y el insider
    -- tiene mejor información. short_pct_float va como fracción
    -- (0.15 = 15% del float), igual que posicion_52w.
    shares_short            bigint,
    short_pct_float         decimal(6, 4),
    fecha_short_interest    date,

    -- Shelf registration activa (S-3 o 424B en los últimos 12 meses,
    -- lo detecta ingesta_13dg.py del mismo índice EDGAR). Si hay shelf
    -- activa, las compras de insiders pueden ser teatro pre-dilución —
    -- es el dato que alimenta el filtro de compra cosmética.
    shelf_activa            boolean default false,
    fecha_ultimo_shelf      date,

    -- Bolsa donde cotiza (NYSE, NASDAQ, AMEX, OTC...). La necesito para
    -- el filtro de Capa 1 que descarta OTC pink sheets — ahí la liquidez
    -- y la calidad de la información suelen ser demasiado bajas para
    -- fiarme de los datos sin verificación adicional.
    bolsa               varchar(20),

    -- Cuándo la detecté y cuándo fue el último update. primera_deteccion
    -- me sirve después para medir cuánto tardó el mercado en valorarla.
    primera_deteccion   timestamp default now(),
    ultimo_update       timestamp default now(),

    -- Estado del pipeline. El orden natural es:
    -- descubierta -> filtros_ok -> scoring_ok -> analizada -> archivada
    -- Si falla algo, va a 'descartada' y guardo el motivo en razon_descarte.
    estado          varchar(20) default 'descubierta'
                    check (estado in (
                        'descubierta', 'filtros_ok', 'scoring_ok',
                        'analizada', 'archivada', 'descartada'
                    )),
    razon_descarte  text,

    -- Borrado lógico para empresas desaparecidas.
    -- Si una empresa deja de presentar informes, la marco como inactiva
    -- en lugar de borrarla físicamente. Así mantengo el historial para
    -- el backtest pero la excluyo del pipeline activo.
    -- El marcado lo hace explícitamente enriquecedor_xbrl.py
    -- (marcar_sin_datos), que es quien sabe el motivo exacto de la baja.
    activa              boolean default true,
    fecha_baja          timestamp,
    motivo_baja         text
);

create index on empresas (ticker);
create index on empresas (estado);
create index on empresas (sic);
-- Este índice es el que más voy a usar en producción: solo quiero
-- empresas activas en el pipeline, todo lo demás es histórico.
create index on empresas (activa, estado);


-- Una fila por trimestre por empresa. El análisis de aceleración
-- lo hago comparando filas consecutivas de esta tabla.
--
-- Decisión importante: no guardo columnas calculadas (growth rates,
-- burn rate como % de revenue). Si los datos fuente cambian, los
-- derivados quedan desfasados. Los calculo en Python al leer.
-- Sí guardo los datos brutos de los que derivan todo.
--
-- Los nombres de columna siguen las etiquetas XBRL de la SEC para
-- que la extracción sea directa y no tenga que hacer mapeos raros.
create table metricas_trimestrales (
    id          serial primary key,
    empresa_id  integer not null references empresas(id) on delete cascade,

    -- Periodo fiscal, no calendario. Q1 de Apple es enero-marzo
    -- pero el Q1 de muchas otras empieza en octubre. Uso año y trimestre
    -- fiscal para evitar confusiones al comparar entre empresas.
    anio_fiscal integer not null,
    trimestre   integer not null check (trimestre between 1 and 4),
    fecha_inicio date,
    fecha_fin    date,

    -- Revenue bruto. Lo que busco no es que sea alto sino que la tasa
    -- de crecimiento esté subiendo trimestre a trimestre. El crecimiento
    -- lo calculo en Python comparando esta columna con la fila anterior.
    revenue             bigint,

    -- Gross profit. Una empresa puede tener pérdidas netas y ser interesante
    -- si el gross margin mejora QoQ — significa que el negocio escala bien.
    gross_profit        bigint,

    -- Beneficio o pérdida neta. Aquí es donde la mayoría de sistemas
    -- descartan empresas que no debería. Amazon perdió dinero 9 años.
    -- Lo que me importa es la tendencia, no el número absoluto.
    net_income          bigint,

    -- El flujo de caja operativo es más honesto que el beneficio contable.
    -- Si el neto es positivo pero el FCO es negativo, hay contabilidad
    -- agresiva. Si el FCO mejora aunque el neto siga en rojo, el negocio
    -- está funcionando aunque no lo parezca en el P&L.
    operating_cash_flow bigint,

    -- Cuentas por cobrar. Si crecen mucho más rápido que el revenue,
    -- es una de las señales de ingresos fantasma que busco. Los días
    -- de cobro (AR days) los calculo en Python: AR / (revenue / 90).
    accounts_receivable bigint,

    -- Total de acciones en circulación. Con esto detecto dilución histórica:
    -- si la empresa emite muchas acciones año tras año, destruye valor
    -- aunque todo lo demás parezca bien.
    shares_outstanding  bigint,

    -- Métricas específicas por sector en JSONB para no tener que añadir
    -- columnas cada vez que incorporo un tipo nuevo de empresa.
    -- Ejemplos reales de lo que guardo aquí:
    --   SaaS:      {"arr": 12000000, "nrr": 115, "cac_payback_months": 14}
    --   Biotech:   {"pipeline_fda_fase": 2, "cash_runway_months": 18}
    --   Retail:    {"same_store_sales_growth": 0.12, "new_locations": 8}
    metricas_sector     jsonb,

    -- El texto narrativo del Item 2 (MD&A) del 10-Q de este trimestre.
    -- Sin esto no tengo nada que embeber ni que darle de comer al LLM
    -- en la Capa 3 — el embedding por sí solo no sirve si no guardo
    -- también el texto original que representa.
    texto_mda           text,

    -- El embedding del texto narrativo del 10-Q de este trimestre.
    -- Pensado para la Capa 4 (RAG, todavía pendiente): cuando analizo
    -- una empresa nueva, buscaría los trimestres históricos más
    -- similares en el espacio vectorial y los usaría como few-shot
    -- para el LLM. Por ahora la columna y el índice están montados,
    -- pero no genero ningún embedding — lo dejé fuera a propósito para
    -- no mezclar "conseguir el texto limpio" con "vectorizarlo" en la
    -- misma decisión (ver el docstring de ingesta_10q.py).
    -- Ojo: la dimensión (1536) hay que confirmarla contra el modelo de
    -- embeddings que se elija de verdad para la Capa 4 — cambiarla
    -- después obliga a recrear la columna y el índice ivfflat.
    embedding           vector(1536),

    unique (empresa_id, anio_fiscal, trimestre)
);

create index on metricas_trimestrales (empresa_id, anio_fiscal, trimestre);
-- Sin este índice pgvector hace full scan. Con miles de trimestres
-- históricos eso es inaceptable. ivfflat con lists=100 es el punto
-- de equilibrio entre velocidad y precisión para este volumen.
create index on metricas_trimestrales
    using ivfflat (embedding vector_cosine_ops) with (lists = 100);


-- Una fila por transacción del Form 4. Una empresa puede tener
-- decenas de filas aquí a lo largo del tiempo.
create table insider_transactions (
    id          serial primary key,
    empresa_id  integer not null references empresas(id) on delete cascade,

    -- El cargo importa: un CFO comprando tiene más peso que un director
    -- independiente porque el CFO ve los números reales cada día.
    nombre_insider  varchar(255),
    cargo           varchar(100),

    -- Todos los códigos de transacción del Form 4 según la SEC.
    -- Los que me importan son P (compra) y S (venta), pero guardo todos
    -- para no perder información. Códigos: P=compra mercado, S=venta,
    -- A=adjudicación, M=ejercicio opciones, F=retención fiscal, G=donación,
    -- X=ejercicio in-the-money, D=disposición, C=conversión, E=expiración,
    -- H, I, J, K, L, O, U, V, W, Z=otros casos menos frecuentes.
    tipo_transaccion    varchar(2)
                        check (tipo_transaccion in (
                            'P','S','A','M','F','G','X','D','C','E',
                            'H','I','J','K','L','O','U','V','W','Z'
                        )),

    -- Segunda confirmación independiente del tipo_transaccion, que viene
    -- de un campo distinto del Form 4 (TRANS_ACQUIRED_DISP_CD). Dice si
    -- el insider ADQUIRIÓ (A) o DISPUSO (D) de los valores. En compras
    -- y ventas normales coincide con lo que ya deduzco de tipo_transaccion,
    -- pero en códigos ambiguos como M (ejercicio de opciones) o A
    -- (adjudicación), este campo me dice si el insider terminó con más
    -- acciones o con menos — algo que tipo_transaccion solo no aclara.
    adquirido_o_dispuesto varchar(1) check (adquirido_o_dispuesto in ('A', 'D')),

    fecha_transaccion   date not null,
    acciones            bigint,
    precio_por_accion   decimal(12, 4),
    importe_total       decimal(15, 2),

    -- Acciones que posee el insider después de la transacción.
    -- Con esto y la compensación del DEF 14A calculo el ratio de
    -- convicción: ¿cuánto representa esta compra en su economía personal?
    acciones_tras_tx    bigint,

    -- La compensación viene del DEF 14A, que descargo por separado.
    -- Si compró el 5% de su sueldo anual es ruido. Si compró el 40%, es
    -- una apuesta real. Esa distinción cambia todo el análisis.
    compensacion_anual  decimal(15, 2),

    -- El ratio de convicción ya calculado. Lo guardo aquí para no
    -- tener que recalcularlo en cada consulta del scoring.
    ratio_conviccion    decimal(8, 4),

    -- El número de acceso del documento en la SEC. Lo guardo por si
    -- necesito releer el XML original para depurar un caso concreto.
    accession_number    varchar(25),

    -- Clave de secuencia dentro del filing (NONDERIV_TRANS_SK en la SEC).
    -- Un mismo Form 4 puede reportar varias transacciones distintas
    -- (ej: el mismo día el insider compra Y ejerce opciones). Sin este
    -- campo, accession_number solo no basta para distinguir cada fila,
    -- y el on conflict de más abajo no tendría nada real que comparar.
    trans_sk            integer,

    fecha_registro      timestamp default now(),

    -- La clave real de una transacción del Form 4 es la combinación de
    -- estos dos campos. Sin este unique, un on conflict do nothing/update
    -- en el loader no tiene ningún conflicto real que detectar — así que
    -- cada vez que se re-ejecuta el loader se duplican todas las filas
    -- en silencio. Este constraint es el que hace que el loader sea
    -- idempotente de verdad, no solo en apariencia.
    unique (accession_number, trans_sk)
);

create index on insider_transactions (empresa_id, fecha_transaccion);
create index on insider_transactions (tipo_transaccion);
-- Este índice es el que uso para detectar el cluster buying:
-- "dame todas las compras P de los últimos 60 días para esta empresa"
create index on insider_transactions (empresa_id, tipo_transaccion, fecha_transaccion);


-- Eventos materiales de los 8-K. Una fila por (filing, item relevante):
-- un mismo 8-K puede traer varios items (ej: "1.01,9.01") y a mí me
-- interesa cada evento por separado, no el documento entero — así cada
-- evento tiene su propio embedding y el Detective puede citar el item
-- concreto que respalda su tesis.
--
-- No guardo TODOS los 8-K a propósito: una small cap presenta 20-40 al
-- año y la mayoría es ruido administrativo (juntas, press releases que
-- duplican el 10-Q). Los items que me interesan están en la tabla
-- configuracion (items_8k_relevantes), no hardcodeados.
create table eventos_8k (
    id          serial primary key,
    empresa_id  integer not null references empresas(id) on delete cascade,

    -- El número de acceso del filing en la SEC, igual que en
    -- insider_transactions: me permite releer el original si un caso
    -- concreto necesita depuración.
    accession_number varchar(25) not null,

    -- El item del 8-K en formato SEC: '1.01', '5.02', etc.
    -- Los que ingiero y por qué:
    --   1.01 = acuerdo material definitivo (contratos, partnerships —
    --          la señal más valiosa para la tesis del proyecto)
    --   1.02 = terminación de acuerdo material (señal negativa que el
    --          red team debe conocer)
    --   2.01 = adquisición o venta de activos
    --   3.02 = venta de acciones no registradas (ampliaciones y
    --          colocaciones — la base del filtro de compra cosmética
    --          del roadmap: cluster de compras justo antes de diluir)
    --   5.02 = salida/nombramiento de directivos (complementa la señal
    --          de insiders: un CFO que compra y luego se va es otra cosa)
    item        varchar(5) not null,

    -- La fecha del EVENTO (reportDate del filing), no la de presentación.
    -- Un 8-K se presenta hasta 4 días hábiles después del evento y a mí
    -- me importa cuándo pasó la cosa, para cruzarla con las fechas de
    -- las compras de insiders.
    fecha_evento date,

    -- Mismo criterio de honestidad que el fallback del MD&A: si no pude
    -- aislar la sección del item y guardé el documento completo
    -- recortado, lo digo aquí en vez de ocultarlo.
    seccion_aislada boolean default true,

    -- El texto de la sección del item. Igual que texto_mda: sin el
    -- texto original, el embedding no sirve para el RAG ni el
    -- verificador de citas tiene contra qué comparar.
    texto       text,

    -- Pensado para el RAG de eventos (Capa 4, pendiente) — mismo estado
    -- que metricas_trimestrales.embedding: columna e índice montados,
    -- todavía sin generar. Misma dimensión para que el proveedor que
    -- elija sirva para los dos corpus a la vez.
    embedding   vector(1536),

    fecha_registro timestamp default now(),

    -- La clave real de un evento es (filing, item). Sin este unique,
    -- re-ejecutar la ingesta duplicaría eventos en silencio — la misma
    -- lección que el unique de insider_transactions.
    unique (accession_number, item)
);

-- "Dame los eventos de esta empresa en los últimos N meses" es la
-- consulta que hace el Detective — este índice la cubre.
create index on eventos_8k (empresa_id, fecha_evento);
create index on eventos_8k
    using ivfflat (embedding vector_cosine_ops) with (lists = 100);


-- Participaciones >5% (Schedule 13D/13G). Es la versión institucional
-- del cluster de insiders: cuando un fondo o activista declara el 5%+
-- de una small cap, hay dinero grande con la misma tesis que mi señal.
-- El 13D además implica intención de influir en la gestión (el 13G es
-- pasivo) — esa distinción la guardo en el formulario.
create table participaciones_activistas (
    id          serial primary key,
    empresa_id  integer not null references empresas(id) on delete cascade,

    -- El accession identifica el filing; aquí es unique directamente
    -- porque un 13D/G no tiene items internos como el 8-K.
    accession_number varchar(25) not null unique,

    -- SCHEDULE 13D/G y sus /A (el nombre nuevo desde finales de 2024;
    -- los viejos eran "SC 13D/G"). Las enmiendas (/A) SÍ las guardo,
    -- al contrario que en los 8-K: en un 13D la enmienda es información
    -- nueva de verdad (subió o bajó su posición).
    formulario  varchar(20) not null,

    fecha_evento date,

    -- El porcentaje declarado, si conseguí extraerlo del texto con
    -- regex. NULL = no lo encontré, el texto queda para que lo lea
    -- el agente de todas formas.
    pct_participacion decimal(5, 2),

    -- El principio del texto del filing (recortado): quién declara,
    -- cuánto y con qué intención. Fuente citable para los agentes.
    texto       text,

    fecha_registro timestamp default now()
);

create index on participaciones_activistas (empresa_id, fecha_evento);


-- Una fila por pasada del pipeline sobre una empresa. Si la analizo
-- en distintos momentos del tiempo, tengo múltiples auditorías.
-- Eso me permite ver cómo evoluciona la señal con el tiempo.
create table auditorias (
    id          serial primary key,
    empresa_id  integer not null references empresas(id) on delete cascade,
    fecha_analisis timestamp default now(),

    -- Los cuatro scores de la Capa 2, desagregados.
    -- Los guardo por separado para poder depurar por qué el sistema
    -- tomó cada decisión, no solo el total.
    score_precio        integer default 0,
    score_conviccion    integer default 0,
    score_temporal      integer default 0,
    score_catalizador   integer default 0,
    score_total         integer default 0,

    -- El veredicto final del pipeline de agentes. Es un nivel de
    -- INTERÉS para investigación, no un consejo de inversión — la
    -- primera versión decía 'SEGURO' y ese nombre prometía algo que
    -- ningún sistema honesto puede prometer:
    --   MUY_INTERESANTE  -> merece investigación humana prioritaria
    --   INTERESANTE      -> señales mixtas, vigilar
    --   NADA_INTERESANTE -> los fundamentales no acompañan a la señal
    --   ALUCINACION      -> el verificador de citas detectó que el
    --                       LLM se inventó datos; análisis descartado
    -- NULL = fila de scoring numérico puro (Capa 2), sin análisis LLM.
    -- El check evita que un typo en el código invente un quinto estado
    -- en silencio — el desajuste 'scored' vs 'scoring_ok' ya me pasó
    -- una vez con empresas.estado y no quiero repetirlo aquí.
    veredicto           varchar(35)
                        check (veredicto is null or veredicto in (
                            'MUY_INTERESANTE', 'INTERESANTE',
                            'NADA_INTERESANTE', 'ALUCINACION'
                        )),

    -- La respuesta completa del LLM en JSON. La guardo entera porque
    -- es la única forma de revisar después si el análisis fue correcto
    -- y ajustar los prompts con los casos reales.
    respuesta_llm       jsonb,

    -- El resultado del verificador de citas. Si una cifra no existe
    -- en el documento original, aquí aparece como CIFRAS_INVENTADAS.
    verificacion_citas  jsonb,

    -- Si esta auditoría pasó por el anonimizador para el backtest,
    -- guardo el factor de rescalado. Solo para auditoría interna,
    -- nunca lo muestro en el dashboard.
    factor_anonimizacion decimal(6, 4),

    -- Qué modelo hizo el análisis. Me sirve para comparar resultados
    -- entre Groq/Llama y Gemini Flash y ver cuál acierta más.
    modelo_llm          varchar(50),

    -- Notas manuales que añado cuando reviso un caso y veo qué pasó
    -- realmente con la empresa en los meses siguientes.
    notas_manuales      text
);

create index on auditorias (empresa_id, fecha_analisis);
create index on auditorias (veredicto);


-- Aquí mido si el sistema funciona de verdad.
-- Comparo el veredicto con lo que pasó realmente después.
-- La referencia a empresa_id la saco de auditorias, no la repito aquí.
create table backtests (
    id              serial primary key,
    auditoria_id    integer not null references auditorias(id) on delete cascade,

    precio_alerta   decimal(12, 4),
    fecha_alerta    date,

    -- Retornos a distintos horizontes. Con esto veo si el sistema
    -- detecta oportunidades de corto, medio o largo plazo.
    retorno_3m      decimal(8, 4),
    retorno_6m      decimal(8, 4),
    retorno_12m     decimal(8, 4),
    retorno_24m     decimal(8, 4),

    -- El Russell 2000 en el mismo periodo es el benchmark honesto.
    -- Si no lo bato, no estoy aportando valor: estaría mejor con un ETF.
    benchmark_retorno_12m   decimal(8, 4),
    batio_benchmark         boolean,

    notas   text
);


-- Parámetros del sistema. Los guardo aquí para no tener constantes
-- hardcodeadas en Python y poder cambiarlos sin tocar código.
create table configuracion (
    clave   varchar(100) primary key,
    valor   text not null,
    notas   text
);

insert into configuracion (clave, valor, notas) values
    ('ventana_trimestres_default', '8',
     'Trimestres de histórico por defecto'),
    ('ventana_trimestres_saas', '6',
     'SaaS crece rápido, con 6 trimestres tengo suficiente señal'),
    ('ventana_trimestres_biotech', '12',
     'Los ciclos FDA son largos, necesito más histórico para ver el patrón'),
    ('score_minimo_llm', '25',
     'Score mínimo sobre 40 para pasar al análisis LLM. Con 30 pasan ~26 empresas, con 25 pasan ~112 — elegí 25 para no perder candidatas por 2 puntos de pesos manuales; el filtro fino lo hacen los agentes'),
    ('market_cap_min', '50000000',   'Mínimo 50M de capitalización'),
    ('market_cap_max', '2000000000', 'Máximo 2B — por encima ya lo ve demasiada gente'),
    ('min_insiders_cluster', '3',    'Mínimo 3 insiders distintos en la ventana de tiempo'),
    ('dias_ventana_cluster', '60',   'Ventana para contar el cluster de compras'),
    ('scheduler_horas', '48',        'Cada cuántas horas corre el scheduler de Form 4'),
    ('meses_sin_datos_baja', '9',
     'Si no hay trimestre nuevo en este tiempo, marco la empresa como inactiva'),
    ('items_8k_relevantes', '1.01,1.02,2.01,3.02,5.02',
     'Items del 8-K que ingiero. El resto (2.02 earnings, 5.07 juntas, 9.01 exhibits) es ruido o duplica el 10-Q'),
    ('meses_ventana_8k', '12',
     'Cuantos meses hacia atras de 8-K descargo por candidata'),
    ('meses_ventana_13dg', '12',
     'Cuantos meses hacia atras de 13D/G descargo por candidata'),
    ('meses_shelf_activa', '12',
     'Un S-3/424B mas reciente que esto marca la shelf como activa'),
    ('max_analisis_por_pasada', '10',
     'Tope de empresas que el orquestador analiza con LLM por pasada — protege las cuotas gratuitas de Groq/Gemini'),
    ('pausa_llm_segundos', '20',
     'Pausa entre analisis LLM del orquestador, por los limites por minuto de los tiers gratuitos');


-- Nota sobre el trigger que ya no existe: la primera versión tenía una
-- función marcar_empresa_inactiva() disparada en cada insert/update de
-- metricas_trimestrales. La quité por tres razones: (1) era redundante —
-- enriquecedor_xbrl.py ya marca las bajas explícitamente y con el motivo
-- exacto; (2) ejecutaba un select max() por cada una de las ~58.000 filas
-- de un backfill; (3) si el backfill insertaba trimestres antiguos antes
-- que los recientes, marcaba como inactiva una empresa viva, y no había
-- ningún camino de reactivación. Si migras una base ya creada con la
-- versión anterior, ejecuta:
--   drop trigger if exists check_empresa_activa on metricas_trimestrales;
--   drop function if exists marcar_empresa_inactiva();


-- Vistas de conveniencia para consultas manuales.
--
-- AVISO que aprendí a base de perder una tarde: una vista con select *
-- congela sus columnas al crearse. Si haces alter table add column
-- después (como pasó con 'bolsa'), la vista NO ve la columna nueva
-- hasta que la recrees. Por eso los scripts del pipeline consultan las
-- tablas directamente con sus condiciones explícitas — estas vistas
-- quedan solo para explorar a mano en psql.
create view empresas_activas as
    select *
    from empresas
    where activa = true
    and estado != 'descartada';

-- Vista para el backtest histórico.
-- Necesito todas las empresas, incluidas las inactivas, porque el
-- patrón histórico de empresas que desaparecieron también es útil
-- para entrenar el modelo (no todo acaba bien).
create view empresas_historico as
    select
        e.*,
        (select max(fecha_fin)
         from metricas_trimestrales mt
         where mt.empresa_id = e.id) as ultimo_trimestre_disponible,
        (select count(*)
         from metricas_trimestrales mt
         where mt.empresa_id = e.id) as total_trimestres
    from empresas e;
