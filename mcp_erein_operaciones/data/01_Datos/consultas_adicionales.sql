-- Sustituir PROJECT_ID y DATASET_ID. Parámetros tipados; nunca SQL libre del agente.
SELECT * FROM `PROJECT_ID.DATASET_ID.transacciones` WHERE caso_id=@caso_id AND fecha BETWEEN @desde AND @hasta ORDER BY fecha, transaccion_id;
SELECT * FROM `PROJECT_ID.DATASET_ID.resumen_cuentas` WHERE caso_id=@caso_id ORDER BY mes;
SELECT * FROM `PROJECT_ID.DATASET_ID.historial_laboral` WHERE caso_id=@caso_id ORDER BY fecha_alta;
