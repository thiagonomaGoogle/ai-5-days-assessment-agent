provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  required_apis = toset([
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "sqladmin.googleapis.com",
    "iamcredentials.googleapis.com",
  ])
}

resource "google_project_service" "agent_apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Dedicated least-privilege runtime service account (zero default compute SA usage)
resource "google_service_account" "agent_runtime" {
  account_id   = "${var.service_name}-sa"
  display_name = "C4 Model Architecture Creator Agent Runtime SA"
  depends_on   = [google_project_service.agent_apis]
}

resource "google_project_iam_member" "agent_runtime_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/dlp.user",
    "roles/cloudsql.client",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# Artifact Registry container repository
resource "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = "${var.service_name}-images"
  description   = "Container images for the C4 Model Architecture Creator ADK Agent"
  format        = "DOCKER"
  depends_on    = [google_project_service.agent_apis]
}

# Managed secrets in Google Cloud Secret Manager (no hardcoded credentials in code or tfvars)
resource "google_secret_manager_secret" "publishing_webhook_token" {
  secret_id = "c4-architect-publishing-webhook-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.agent_apis]
}

resource "google_secret_manager_secret" "session_db_uri" {
  secret_id = "c4-architect-session-db-uri"
  replication {
    auto {}
  }
  depends_on = [google_project_service.agent_apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_webhook_secret_access" {
  secret_id = google_secret_manager_secret.publishing_webhook_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "runtime_db_secret_access" {
  secret_id = google_secret_manager_secret.session_db_uri.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# Persistent Cloud SQL PostgreSQL 16 instance for ADK DatabaseSessionService
resource "google_sql_database_instance" "session_store" {
  name                = "${var.service_name}-sessions-${var.environment}"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = "db-custom-1-3840"
    availability_type = "REGIONAL"

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ssl_mode = "ENCRYPTED_ONLY"
    }
  }

  depends_on = [google_project_service.agent_apis]
}

resource "google_sql_database" "adk_sessions_db" {
  name     = "c4_adk_sessions"
  instance = google_sql_database_instance.session_store.name
}

# Cloud Run v2 Service hosting the ADK Multi-Agent API Server
resource "google_cloud_run_v2_service" "c4_agent_service" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.agent_runtime.email

    scaling {
      min_instance_count = 1
      max_instance_count = 10
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "C4_ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "C4_ENABLE_CLOUD_TRACE"
        value = "TRUE"
      }
      env {
        name  = "C4_ENABLE_CLOUD_DLP"
        value = "TRUE"
      }

      # Inject secrets securely from Secret Manager at runtime
      env {
        name = "C4_MOUNTED_DB_URI"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_db_uri.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "C4_PUBLISHING_WEBHOOK_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.publishing_webhook_token.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.runtime_db_secret_access,
    google_secret_manager_secret_iam_member.runtime_webhook_secret_access,
  ]
}
