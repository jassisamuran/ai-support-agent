# AI Support Agent Platform

[![Ask DeepWiki](https://devin.ai/assets/askdeepwiki.png)](https://deepwiki.com/jassisamuran/ai-support-agent)

This repository contains the backend for an enterprise-grade, multi-tenant AI support agent designed for e-commerce platforms. The agent can handle a wide range of customer queries, from checking order statuses and managing support tickets to answering policy questions using a knowledge base.

It is built with a focus on performance, resilience, and extensibility, featuring sophisticated caching mechanisms, tool-based function calling, and a resilient architecture that can fall back to secondary services.

## Core Features

*   **Intelligent Agent Core**: A sophisticated agent (`agent.py`) orchestrates interactions between the user, LLMs, and a suite of tools.
*   **Dynamic Tool Use**: The agent can use a variety of tools to interact with external systems, such as:
    *   Listing orders and support tickets.
    *   Checking the status of specific orders.
    *   Creating new support tickets.
    *   Comparing product specifications.
    *   Cancelling orders and initiating refunds.
*   **Stateful Pagination**: Efficiently handles navigation (`next`, `previous`, `go to page X`) for large lists of orders or tickets using a Redis-backed pagination cache, avoiding redundant API and LLM calls.
*   **Semantic Caching**: Caches responses for common, non-user-specific queries (e.g., "what is the return policy?") to reduce latency and API costs.
*   **Retrieval-Augmented Generation (RAG)**: Ingests documents (like PDFs) into a ChromaDB vector store to provide accurate answers from a private knowledge base.
*   **Resilient LLM Service**: A circuit breaker pattern automatically falls back from the primary LLM (OpenAI) to a secondary provider (Anthropic) in case of API failures.
*   **Multi-Tenancy**: Supports multiple organizations, each with its own configuration, API keys, system prompts, and isolated knowledge base.
*   **Asynchronous Task Processing**: Uses `arq` with Redis to handle background jobs like document ingestion, ensuring the API remains responsive.
*   **Usage and Cost Tracking**: Monitors token usage and associated costs for each organization, enabling billing and budget management.
*   **Webhook Support**: Can dispatch events (e.g., `conversation.started`, `ticket.created`) to external systems for further integration.

## Technical Architecture

The platform is built on a modern Python stack, containerized for easy deployment and scalability.

*   **Web Framework**: **FastAPI** for high-performance, asynchronous API endpoints.
*   **Database**: **PostgreSQL** with **SQLAlchemy** for data modeling and persistence.
*   **LLM Providers**: **OpenAI** (primary) and **Anthropic** (fallback).
*   **Vector Database**: **ChromaDB** for storing embeddings for the RAG system.
*   **Caching & Task Queues**: **Redis** is used for:
    *   Pagination state management.
    *   Semantic caching of LLM responses.
    *   API rate limiting.
    *   `arq` background job queuing.
*   **Containerization**: **Docker** and **Docker Compose** for consistent development and production environments.

### Key Components

*   `app/core/agent.py`: The central orchestrator that manages conversational flow, tool execution, and caching logic.
*   `app/core/tools.py`: Defines the functions (tools) the agent can call to interact with external services (e.g., an e-commerce backend).
*   `app/core/pagination_cache.py`: Implements the Redis-based state management for paginated data, enabling fast, stateless navigation.
*   `app/core/llm_service.py`: A robust wrapper for LLM API calls, featuring the circuit breaker for resilience.
*   `app/core/rag.py`: Handles document chunking, embedding, and retrieval from the ChromaDB vector store.
*   `app/workers/`: Contains the logic for background tasks, primarily for ingesting documents into the knowledge base.
*   `app/models/`: Defines the SQLAlchemy data models for organizations, users, conversations, tickets, and billing events.
*   `app/api/v1/`: Contains the FastAPI routers for different API resources like `chat`, `auth`, and `webhooks`.
*   `docker-compose.prod.yml`: Defines the services for production deployment, including the backend application, worker, and ChromaDB.

## Getting Started

### Prerequisites

*   Docker
*   Docker Compose

### Installation and Setup

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/jassisamuran/ai-support-agent.git
    cd jassisamuran-ai-support-agent/bacend/
    ```

2.  **Configure Environment Variables**
    Create a file named `.env.prod` in the `bacend` directory and populate it with the necessary configuration. Refer to `app/config.py` for all possible variables.

    **Example `.env.prod`:**
    ```env
    SECRET_KEY=your_super_secret_key_for_jwt
    DATABASE_URL=postgresql+asyncpg://user:password@host:port/dbname
    SYNC_DATABASE_URL=postgresql+psycopg2://user:password@host:port/dbname
    
    # LLM and Services API Keys
    OPENAI_API_KEY=sk-..................
    ANTHROPIC_API_KEY=sk-ant-.................. # Optional, for fallback
    
    # Service URLs
    REDIS_URL=redis://localhost:6379
    CHROMA_HOST=chromadb
    CHROMA_PORT=8000
    BACKEND_API=http://your-ecommerce-backend-api # URL for the backend the tools will call
    
    # Model Configuration
    OPENAI_MODEL=gpt-4o-mini
    ANTHROPIC_MODEL=claude-3-haiku-20240307
    OPENAI_EMBEDDING_MODEL=text-embedding-3-small
    CHROMA_COLLECTION=main_collection
    
    # Billing/Cost
    GPT4O_MINI_INPUT_COST_PER_1M=0.15
    GPT4O_MINI_OUTPUT_COST_PER_1M=0.60
    
    # Circuit Breaker
    CB_FAILURE_THRESHOLD=3
    CB_RECOVERY_TIMEOUT=60
    ```

3.  **Run with Docker Compose**
    From the `bacend` directory, start the services:
    ```bash
    docker-compose -f docker-compose.prod.yml up --build
    ```
    This will build the Docker images and start three containers:
    *   `ai_backend`: The main FastAPI application.
    *   `ai_worker`: The `arq` worker for background tasks.
    *   `ai_chromadb`: The ChromaDB vector store instance.

The API will be available at `http://localhost:8000`.

## API Endpoints

*   `POST /api/v1/chat/message`: The primary endpoint for interacting with the AI agent. Send user messages here to get a response.
*   `POST /api/v1/auth/register`: Create a new organization and owner user.
*   `POST /api/v1/chat/session`: Create a new conversation session.
*   `GET /api/v1/chat/latest-messages`: Retrieve messages for a given conversation ID.
*   `GET /health`: A health check endpoint to verify the service is running.
