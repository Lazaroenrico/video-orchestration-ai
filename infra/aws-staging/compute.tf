resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/ugc-orchestrator/staging/api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "runner" {
  name              = "/ecs/ugc-orchestrator/staging/runner"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "this" {
  name = "ugc-orchestrator-staging"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

locals {
  common_environment = [
    { name = "AWS_REGION", value = var.aws_region },
    { name = "ORCH_AUTH_MODE", value = "cloudflare_access" },
    { name = "ORCH_CONFIG_DIR", value = "/app/config-staging" },
    { name = "ORCH_ENABLE_PAID_ADAPTERS", value = "false" },
    { name = "ORCH_ENV", value = "staging" },
    { name = "ORCH_ORGANIZATION_NAME", value = var.organization_name },
    { name = "ORCH_ORGANIZATION_SLUG", value = var.organization_slug },
    { name = "ORCH_QUEUE_BACKEND", value = "sqs" },
    { name = "ORCH_SERVE_LOCAL_MEDIA", value = "0" },
    { name = "ORCHESTRATOR_LOG_FORMAT", value = "json" },
    { name = "S3_BUCKET", value = aws_s3_bucket.media.id },
    { name = "SQS_QUEUE_URL", value = aws_sqs_queue.wake.url },
    { name = "STORAGE_BACKEND", value = "dual" },
    { name = "STORAGE_WRITE_BACKEND", value = "s3" },
    { name = "R2_ACCOUNT_ID", value = var.r2_account_id },
    { name = "R2_BUCKET", value = var.r2_bucket },
    {
      name  = "R2_ENDPOINT_URL"
      value = "https://${var.r2_account_id}.r2.cloudflarestorage.com"
    },
    { name = "CF_ACCESS_TEAM_DOMAIN", value = var.cloudflare_access_team_domain },
    { name = "CF_ACCESS_AUDIENCE", value = var.cloudflare_access_audience },
  ]
  common_secrets = [
    { name = "DATABASE_URL", valueFrom = var.database_secret_arn },
    { name = "R2_ACCESS_KEY_ID", valueFrom = var.r2_access_key_secret_arn },
    { name = "R2_SECRET_ACCESS_KEY", valueFrom = var.r2_secret_key_secret_arn },
  ]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "ugc-orchestrator-staging-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name       = "api"
    image      = local.immutable_image
    essential  = true
    entryPoint = ["orchestrator", "api"]
    command    = ["--host", "0.0.0.0", "--port", "8000"]
    environment = concat(local.common_environment, [
      { name = "ORCH_CORS_ORIGINS", value = var.app_origin },
    ])
    secrets = local.common_secrets
    portMappings = [{
      name          = "http"
      containerPort = 8000
      protocol      = "tcp"
      appProtocol   = "http"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "runner" {
  family                   = "ugc-orchestrator-staging-runner"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 4096
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  ephemeral_storage {
    size_in_gib = 30
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name       = "runner"
    image      = local.immutable_image
    essential  = true
    entryPoint = ["orchestrator", "sqs-runner"]
    environment = concat(local.common_environment, [
      { name = "ORCH_USER_SUBJECT", value = var.runner_subject },
    ])
    secrets = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.runner.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "runner"
      }
    }
  }])
}

resource "aws_security_group" "alb" {
  name        = "ugc-orchestrator-staging-alb"
  description = "TLS ingress; Cloudflare permanece como borda durante o cutover."
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "tasks" {
  name   = "ugc-orchestrator-staging-tasks"
  vpc_id = var.vpc_id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "api" {
  name               = "ugc-orchestrator-staging"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "api" {
  name                 = "ugc-orchestrator-staging"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 300

  health_check {
    enabled             = true
    path                = "/readyz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "ugc-orchestrator-staging-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "runner" {
  name            = "ugc-orchestrator-staging-runner"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.runner.arn
  desired_count   = var.runner_desired_count
  launch_type     = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
}
