resource "aws_cloudwatch_metric_alarm" "wake_dlq" {
  alarm_name          = "ugc-staging-sqs-dlq-visible"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arns

  dimensions = {
    QueueName = aws_sqs_queue.wake_dlq.name
  }
}

resource "aws_cloudwatch_metric_alarm" "wake_age" {
  alarm_name          = "ugc-staging-sqs-oldest-message"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 120
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arns

  dimensions = {
    QueueName = aws_sqs_queue.wake.name
  }
}

resource "aws_cloudwatch_metric_alarm" "runner_cpu" {
  alarm_name          = "ugc-staging-runner-cpu"
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arns

  dimensions = {
    ClusterName = aws_ecs_cluster.this.name
    ServiceName = aws_ecs_service.runner.name
  }
}
