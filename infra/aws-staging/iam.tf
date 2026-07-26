data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "ugc-orchestrator-staging-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      var.database_secret_arn,
      var.r2_access_key_secret_arn,
      var.r2_secret_key_secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-runtime-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "ugc-orchestrator-staging-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.media.arn}/*"]
  }

  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.media.arn]
  }

  statement {
    actions = [
      "sqs:ChangeMessageVisibility",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ReceiveMessage",
      "sqs:SendMessage",
    ]
    resources = [aws_sqs_queue.wake.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "media-and-wake-queue"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
