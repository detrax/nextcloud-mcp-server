resource "aws_security_group" "efs" {
  name        = "${var.name}-efs"
  description = "NFS ingress from ${var.name} ECS tasks"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "efs_from_task" {
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  description                  = "NFS from task"
}

# EFS mount targets accept NFS from any SG referenced by the EFS SG. The
# mcp-server task SG covers itself; the qdrant task SG (when present) gets
# its own ingress rule.
resource "aws_vpc_security_group_ingress_rule" "efs_from_qdrant" {
  count                        = var.use_external_qdrant ? 0 : 1
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.qdrant[0].id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  description                  = "NFS from qdrant task"
}

resource "aws_efs_file_system" "this" {
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = {
    Name = var.name
  }
}

resource "aws_efs_mount_target" "this" {
  for_each = toset(var.private_subnet_ids)

  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# Container runs as root (upstream Dockerfile has no USER directive), so the
# access points stamp uid/gid 0 on created files.
resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/data"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }

  tags = {
    Name = "${var.name}-data"
  }
}

resource "aws_efs_access_point" "oauth" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/oauth"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }

  tags = {
    Name = "${var.name}-oauth"
  }
}

# Qdrant container runs as root (debian-slim base, no USER directive), matching
# the other access points. Mounted at /qdrant/storage which is qdrant's default
# storage_path.
resource "aws_efs_access_point" "qdrant" {
  count          = var.use_external_qdrant ? 0 : 1
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/qdrant"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }

  tags = {
    Name = "${var.name}-qdrant"
  }
}
