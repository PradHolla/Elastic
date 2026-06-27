resource "aws_ecr_repository" "api" {
  name         = "${local.name_prefix}/api"
  force_delete = var.force_delete_ecr_repositories

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "worker" {
  name         = "${local.name_prefix}/worker"
  force_delete = var.force_delete_ecr_repositories

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "web" {
  name         = "${local.name_prefix}/web"
  force_delete = var.force_delete_ecr_repositories

  image_scanning_configuration {
    scan_on_push = true
  }
}
