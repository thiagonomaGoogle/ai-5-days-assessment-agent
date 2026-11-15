output "cloud_run_service_uri" {
  description = "Deployed HTTPS endpoint of the C4 Model Architecture Creator Agent."
  value       = google_cloud_run_v2_service.c4_agent_service.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege runtime IAM service account."
  value       = google_service_account.agent_runtime.email
}

output "artifact_registry_repository" {
  description = "Artifact Registry Docker repository ID."
  value       = google_artifact_registry_repository.agent_repo.id
}

output "session_sql_connection_name" {
  description = "Cloud SQL PostgreSQL connection name backing ADK DatabaseSessionService."
  value       = google_sql_database_instance.session_store.connection_name
}
