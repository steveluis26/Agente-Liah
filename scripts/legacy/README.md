# Scripts legacy (Fase 0) — NO usar como referencia para el vertical actual

Estos scripts pertenecen al demo original de la academia de danza ("Baila Ya")
y contienen literales del vertical hardcodeados (`salsa`/`bachata`/`ballet`,
`trial_class`, `colegiatura`).

- `demo_academia.py`: la Fase 5 la reescribe como demo de **consultorio
  médico** end-to-end. Sus PASS son engañosos (validan retrieval, no la
  respuesta final) — no heredar su lógica.
- `demo_tests_extra.py`, `_verify_demo_pipeline.py`: verificaciones ad-hoc
  del pipeline Fase 0.
- `seed_fase0.py`: seed del tenant `academia-danza-demo`.

Además, hacen `drop_all()` sin exigir `LIAH_DEMO_RESET=1` explícito:
correrlos contra la URL equivocada borra la BD a la que apunten. Se
conservan solo como archivo histórico; la Fase 5 escribe sus propios
scripts sobre el mecanismo de plantillas de `templates/`.
