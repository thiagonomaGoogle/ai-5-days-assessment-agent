variable "project_id" {
  description = "Target Google Cloud Project ID hosting the C4 Architect Agent."
  type        = string
}

variable "region" {
  description = "Primary Google Cloud region for Cloud Run, Artifact Registry, and Vertex AI."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name for the C4 Model Architecture Creator Agent."
  type        = string
  default     = "c4-architect-agent"
}

variable "container_image" {
  description = "Immutable container image digest deployed to Cloud Run."
  type        = string
}

variable "environment" {
  description = "Deployment environment label (development, staging, production)."
  type        = string
  default     = "production"
}

variable "github_repository" {
  description = "GitHub org/repo slug authorized via Workload Identity Federation (e.g. 'org/ai-5-days-assessment-agent')."
  type        = string
  default     = "experimental/ai-5-days-assessment-agent"
}
