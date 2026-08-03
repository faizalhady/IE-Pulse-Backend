# OLE Backend Reorganization Plan

This document outlines the strategy for refactoring and reorganizing the `IE-Pulse-Backend` codebase to improve maintainability, scalability, and code quality.

## 1. Objectives
*   **Modularization:** Decouple the API monolith and pipeline logic.
*   **Separation of Concerns:** Distinct layers for Routing, Business Logic (Services), and Data Access.
*   **Maintainability:** Extract complex SQL and configuration into dedicated files.
*   **Project Hygiene:** Clean up the root directory and establish testing/tooling standards.

## 2. Target Directory Structure

```text
IE-Pulse-Backend/
├── api/
│   ├── endpoints/          # Route handlers (FastAPI APIRouter)
│   │   ├── ole.py          # /api/ole, /api/ole/summary, etc.
│   │   ├── operational.py  # /api/downtime, /api/transfers
│   │   └── system.py       # /api/health, /api/refresh, /api/workcells
│   ├── models/             # Pydantic schemas for request/response
│   ├── services/           # Heavy lifting (Forecasting, Pareto, OLE Math)
│   ├── core/               # App configuration, security, and middleware
│   ├── utils.py            # API-specific helpers (Parquet views, JSON cleaning)
│   └── main.py             # FastAPI entry point & app initialization
├── pipeline/
│   ├── sql/                # External .sql files for DuckDB
│   │   ├── compute_ole.sql
│   │   └── compute_weekly.sql
│   ├── compute.py          # Runner for transformation logic
│   ├── ingest.py           # Data normalization & mart ingestion
│   └── refresh.py          # Orchestrator for full pipeline runs
├── tools/                  # Diagnostic and utility scripts
│   ├── diagnose.py
│   ├── naming.py
│   └── mapping_template.json
├── tests/                  # Pytest suite
│   ├── conftest.py
│   ├── test_api/
│   └── test_pipeline/
├── data/                   # (Ignored by Git) Raw and Mart storage
├── .env.example            # Template for environment variables
├── config.py               # Global settings and hardcoded mappings
├── database.py             # SQLite connection management
└── README.md
```

## 3. Key Changes

### A. API Refactoring (The "Monolith" Split)
*   **Current State:** `api/main.py` handles everything from SQL generation to route definitions and data cleaning.
*   **Change:** Move routes into `api/endpoints/` using `fastapi.APIRouter`. Move the predictive logic (ARIMA/Holt-Winters) and complex aggregations (Pareto, MH Breakdown) into `api/services/`.

### B. Pipeline SQL Externalization
*   **Current State:** Large, multi-line SQL strings are embedded in Python files.
*   **Change:** Store these in `pipeline/sql/*.sql`. Use a helper to load and execute them via DuckDB, allowing for syntax highlighting and easier debugging.

### C. Configuration & Environment
*   **Current State:** Network paths and directory settings are hardcoded in `config.py`.
*   **Change:** Implement `python-dotenv`. Create a `.env.example` to guide users on setting up local/production network paths.

### D. Data Validation
*   **Current State:** Basic type casting during ingestion.
*   **Change:** Use Pydantic models in the API to validate inputs. Consider using `pandera` in the pipeline to validate Parquet schemas before they hit the mart.

### E. Root Directory Cleanup
*   **Current State:** Scripts like `diagnose.py` and `naming.py` are at the top level.
*   **Change:** Move these to a `tools/` directory to keep the root focused on project metadata and core entry points.

## 4. Implementation Phases

1.  **Phase 1 (Infrastructure):** Create directory structure, move tool scripts, and setup `.env`.
2.  **Phase 2 (API Routes):** Split `api/main.py` into `endpoints/` and `utils.py`.
3.  **Phase 3 (Services):** Extract prediction and complex math into the `services/` layer.
4.  **Phase 4 (Pipeline):** Externalize SQL and improve error handling in `ingest.py`.
5.  **Phase 5 (Testing):** Add baseline tests for critical OLE calculations.
