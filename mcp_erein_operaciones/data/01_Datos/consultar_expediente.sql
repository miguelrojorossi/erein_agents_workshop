-- Sustituir PROJECT_ID y DATASET_ID; parámetro @caso_id STRING.
SELECT AS STRUCT
  e.*,
  ARRAY(SELECT AS STRUCT v.evidencia_id, v.caso_id, v.tipo_fuente,
    v.fecha_evidencia, v.periodo, v.disponibilidad,
    IF(v.tipo_fuente = 'Nómina', NULL, v.importe_eur) AS importe_eur,
    IF(v.tipo_fuente = 'Nómina', 'Leer el PDF vinculado o solicitarlo si está ausente', v.contenido) AS contenido
    FROM `PROJECT_ID.DATASET_ID.evidencias` v WHERE v.caso_id = e.caso_id) AS evidencias,
  ARRAY(SELECT AS STRUCT x.* FROM `PROJECT_ID.DATASET_ID.documentos` x
    WHERE x.caso_id=e.caso_id AND x.disponible_inicial=TRUE) AS documentos
FROM `PROJECT_ID.DATASET_ID.expedientes` e WHERE e.caso_id=@caso_id;

-- La política se recupera desde RAG. politica.csv es referencia de preparación.
-- Crear acciones usando acciones.schema.json.
