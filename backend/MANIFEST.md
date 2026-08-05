# ARIA — Манифест сборки (auto-generated)
**Дата**: 2026-07-27
**Версия**: 0.0.0 (dev)
**Status**: ok

## Системные проверки
- ✅ **database**: ok
- ✅ **models**: ok (15 tables)
- ✅ **router**: ok (9 providers, 3 classes)
- ✅ **config**: ok
- ✅ **provider_catalog**: 177 models cached
- ✅ **vault**: ok
- ✅ **skills**: 336 skills in skills_meta

## Дерево файлов

```
         27K  agent-real-loop.patch
    alembic/
          2K      env.py
         38B      README
        704B      script.py.mako
        versions/
        742B          00dc62eaabfc_initial_schema.py
          5K  alembic.ini
    aria/
          0B      __init__.py
        agents/
          0B          __init__.py
          9K          codex_exec_bridge.py
        api/
          0B          __init__.py
          2K          auth.py
          1K          http.py
        349B          ws.py
        168K      app.zip
          7K      config.py
        core/
          0B          __init__.py
          3K          approvals.py
          6K          audit.py
          3K          delegate.py
          3K          events.py
         15K          loop.py
        578B          rate_limit.py
          6K          roles.py
        448B          state_machine.py
        db/
         91B          __init__.py
          2K          base.py
          4K          enums.py
         16K          models.py
         17K          repository.py
        llm/
          0B          __init__.py
          7K          compression.py
          3K          key_pool.py
            providers/
          0B              __init__.py
        971B              base.py
          6K              openai_compatible.py
          2K              stub.py
         10K          router.py
         46K      main.py
        prompts/
         60B          general.md
         56B          qa_auditor.md
        routers/
          2K          providers.py
          3K          storage.py
        scheduler/
         25B          __init__.py
          3K          jobs.py
        storage/
          0B          __init__.py
          7K          b2_client.py
          8K          obsidian_vault.py
            postgres/
        290B              __init__.py
            redis/
        304B              __init__.py
        tools/
          0B          __init__.py
            handlers/
          0B              __init__.py
          2K              files.py
          2K              shell.py
          2K              web.py
         15K          registry.py
          3K          validators.py
          2K  backend.spec
        398K  backend.zip
    build/
        backend/
        362K          Analysis-00.toc
       22.0M          backend.pkg
        1.4M          base_library.zip
         45K          EXE-00.toc
            localpycs/
         43K          PKG-00.toc
       12.0M          PYZ-00.pyz
        266K          PYZ-00.toc
         18K          warn-backend.txt
        2.6M          xref-backend.html
    data/
        288K      local_agent.db
         32K      local_agent.db-shm
         44K      local_agent.db-wal
        vault/
            00-RAW/
                агент/
         12K                  memory-dump-2026-07-10.md
          5K                  memory-dump-2026-07-13.md
          7K                  memory-dump-2026-07-14.md
            03-PROJECTS/
                AI-Influencer/
          2K                  2026-07-13-phasea-v5-backup-restore.md
         26K                  audit-b2-2026-07-13.md
            AGENTS/
         11K              AI-агенты.md
          9K              Swarm Оркестратор.md
          3K              Как это работает.md
            COMFY-KB/
                generation-log/
          2K                  2026-07-08-b2-test-generations.md
          3K              INDEX.md
                models/
                    checkpoints/
          2K                      bigLust_v16.md
          1K                      Lustify_apexV8.md
                    controlnet/
          2K                      controlnet-union-sdxl-1.0.md
          1K                      instantid_controlnet.md
          1K                      openpose-sdxl-1.0.md
                    detectors/
          1K                      face_yolov8m.md
          1K                      hand_yolov8n.md
                    ipadapter/
          1K                      ip-adapter-faceid-plusv2_sdxl.md
          1K                      ip-adapter-plus-face_sdxl_vit-h.md
          1K                      ip-adapter_sdxl_vit-h.md
                    loras/
        998B                      DetailedEyes_V3.md
          1K                      HandsXL_v1.md
          1K                      RealSkin_xxXL_v1.md
                nodes/
          5K                  ApplyInstantID.md
          6K                  ApplyInstantIDAdvanced.md
          4K                  ControlNetApplyAdvanced.md
          5K                  FaceDetailer.md
          5K                  HandsDetailer.md
          7K                  IPAdapterFaceID.md
          4K                  KSampler.md
          3K                  UltralyticsDetectorProvider.md
                params/
          2K                  cfg-guidance.md
          3K                  controlnet-strength.md
          2K                  denoise-strength.md
          3K                  ipadapter-weight.md
          4K                  sampler-scheduler.md
                postprocessing/
          3K                  face-restore.md
          2K                  frequency-separation.md
          2K                  upscale.md
                quality-and-consistency/
          4K                  face-similarity-playbook.md
          6K                  failure-patterns.md
          2K                  known-good-configs.md
            CONTENT/
          4K              INDEX.md
         11K              production-plan-v2.md
                STORY-DATABASE/
                    Girl-Trans/
          1K                      INDEX.md
          2K                  INDEX.md
                    Lesbian/
        955B                      INDEX.md
                    Solo/
                        Bedroom/
          8K                          SASHA-SO-008-found-it.md
                        Car/
          9K                          MIA-SO-008-parking-garage.md
                        Fitting-Room/
                        Home-Tease/
          1K                      INDEX.md
                        Innocent-Slutty/
                        Mirror/
                        Morning-Routine/
                        Office-Study/
          7K                          MIA-SO-009-overtime.md
                        Outdoor/
          7K                          SASHA-SO-009-the-bench.md
                    Tags/
          2K                      INDEX.md
                    Top50/
        657B                      INDEX.md
                    Trans/
          1K                      INDEX.md
          8K                      NADIA-SO-003-the-mirror.md
          7K                      NADIA-SO-004-the-stall.md
                STORY-TEMPLATE/
          5K                  INDEX.md
          3K                  quality-gates.md
                TREND-RESEARCH/
         10K                  2026-porn-trends-analysis.md
          3K                  INDEX.md
          6K                  studio-archetype-catalog.md
            DECISIONS/
         13K              Decision-Flow.md
          6K              Decision-Log.md
          3K              HERMES.md
          2K              Known-Risks.md
            GODTIER/
          4K              2026-07-06-face-lora-bootstrap.md
         11K              AI Influencer Pipeline.md
         17K              AI-Adult-Content-Bible-2026.md
          2K              GODTIER V8 Project Spec.md
         17K              GODTIER V8 Workflow Spec.md
          6K              GODTIER V8.md
          4K              golden_instantid_restore.md
                LEGACY-AUDIT/
         46K                  01-runtime-legacy-audit-full.md
        257K                  02-runtime-legacy-audit-part2-full.md
        629B                  INDEX.md
                sdxl-pipeline/
                    brunette/
          8K                      dataset_validator.py
          6K                      gen_batch7.py
         43K                      gen_phaseA_sdxl.py
          9K                      gen_phaseB_sdxl.py
         16K                      MASTER-POD-CHECKLIST.md
         20K                      post_process.py
         18K                      prompts_brunette.yaml
         13K                      quality_scorer.py
          7K                      README.md
         11K                      tag_dataset.py
          4K                      unified_config_brunette.yaml
         10K          INDEX.md
            PIPELINE/
          3K              AI Видео Генерация.md
          4K              Checkpoint Parameters.md
          6K              Checkpoint_Merging.md
         11K              ComfyUI.md
          5K              ComfyUI_Workflow_Nodes.md
          5K              ControlNet_IPAdapter_PuLID.md
          5K              Dataset_Captioning.md
          6K              DoRA_Parameters.md
        521B              FaceFusion.md
          5K              Generation_Parameters.md
         14K              LoRA Dataset Guide.md
          7K              LoRA_Parameters.md
          3K              SDXL Big Love Prompts.md
          4K              SDXL Big Love.md
          1K              Template-vs-No-Template.md
          4K              Upscalers.md
          3K              Vision Fallback (LLM).md
          5K              VRAM_Optimization_3090.md
            PROJECTS/
                AI-Influencer/
          5K                  01-Gen-Stack.md
          7K                  02-RunPod-Deploy.md
          2K                  03-Patches.md
          5K                  04-PostProcessing.md
          7K                  05-B2-Backup.md
          6K                  06-Scripts.md
          8K                  07-Decision-Log.md
          8K                  08-ComfyUI-Architecture.md
          9K                  09-Deploy-Procedure.md
          4K                  12-Generation-2026-07-08.md
          4K                  13-What-To-Try-Next.md
          5K                  14-Quality-Analysis-2026-07-08.md
          3K                  README.md
          4K              B2 Readiness Report.md
         13K              Business Plans.md
          3K              Deploy 2 Plan.md
         11K              test_matrix.py
          5K              Testing Matrix.md
            SIGNALS/
          3K              Loop-Engineering.md
          2K              Quant Trading.md
            STACK/
          2K              B2 Database Inventory.md
          2K              B2 Debugging UV Zombies.md
          8K              B2 Storage.md
          8K              Deploy Script.md
        530B              DOCKER_TROUBLESHOOTING.md
         11K              Golden Deploy.md
          8K              Hermes Agent.md
          2K              Hermes Error Troubleshooting.md
          1K              Killswitch-Cronjob.md
          5K              n8n-MCP проект.md
          2K              n8n-MCP.md
        545B              N8N_DEPLOYMENT.md
        477B              RAILWAY_DEPLOYMENT.md
         21K              RunPod-Playbook.md
          9K              RunPod.md
        638B              SELF_HOSTING.md
          5K              Storage_B2_Structure.md
          8K              STT Speech-to-Text.md
          7K              Telegram_Bot_HyperAgent.md
          7K              TG Gateway.md
          2K              Windows Специфика.md
          1K              Командный Центр CC v3.md
            Templates/
        137B              MOC-страница.md
        165B              RAW-заметка.md
        202B              Wiki-страница.md
            VAULT/
          9K              Second-Brain.md
          4K              Statuses.md
        432B              Линковка.md
          6K              Удалённые-проекты.md
    dist/
        aria-backend/
            aria-backend/
                _internal/
        1.4M                  base_library.zip
       22.3M      backend.exe
          2K  import_skills.py
          1K  logging.yaml
    logs/
         80K      backend.log
        522B  requirements.txt
          1K  run_aria.py
        408B  run_backend.py
    scripts/
          3K      generate_manifest.py
          2K  start_aria.sh
    tests/
          0B      __init__.py
          1K      test_auth_bootstrap.py
        557B      test_config_contract.py
          6K      test_delegate_task.py
          3K      test_provider_catalog.py
        381B      test_scheduler_jobs.py
          3K      test_vault_and_skills.py
```
