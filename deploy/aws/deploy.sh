#!/bin/bash
# Deploy Thravi OncoCare to AWS at https://onco.thravi.in (ap-south-1, Mumbai).
#
#   ./deploy/aws/deploy.sh            first run: creates everything, then deploys; later runs: redeploy
#
# Uses your normal AWS CLI login (AWS_PROFILE is honoured). Deploys the last git commit (git archive HEAD).
# Creates (idempotently): S3 bucket for releases, SSM SecureString parameters for secrets, IAM role for the
# instance (SSM management only), security group (80/443 only, no SSH), Elastic IP, Route 53 A record,
# and one Ubuntu 24.04 ARM instance. Caddy on the instance provides HTTPS via Let's Encrypt.
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
DOMAIN="${DOMAIN:-onco.thravi.in}"
ZONE_NAME="${ZONE_NAME:-thravi.in}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.micro}"
NAME=oncocare
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" AWS_PAGER=""

cd "$(dirname "$0")/../.."
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
command -v aws >/dev/null || die "AWS CLI v2 is not installed (https://aws.amazon.com/cli/)."
command -v git >/dev/null || die "git is not installed."

ACCOUNT=$(aws sts get-caller-identity --query Account --output text) || die "AWS CLI is not signed in."
say "AWS account $ACCOUNT, region $REGION, domain $DOMAIN"
BUCKET="$NAME-deploy-$ACCOUNT-$REGION"

# ---------------------------------------------------------------- S3 bucket for release tarballs
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  say "Creating private S3 bucket $BUCKET"
  aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

# ---------------------------------------------------------------- secrets in SSM Parameter Store
if ! aws ssm get-parameter --name /oncocare/demo_password >/dev/null 2>&1; then
  say "Generating the demo sign-in password"
  PW="onco-$(openssl rand -hex 6)"
  aws ssm put-parameter --name /oncocare/demo_password --type SecureString --value "$PW" >/dev/null
fi
if ! aws ssm get-parameter --name /oncocare/anthropic_api_key >/dev/null 2>&1 && [ -f .env ]; then
  KEY=$(sed -n 's/^ANTHROPIC_API_KEY=//p' .env | tail -1 | tr -d '"'"'"' ')
  if [ -n "$KEY" ]; then
    say "Storing ANTHROPIC_API_KEY from .env in SSM (for AI lab extraction)"
    aws ssm put-parameter --name /oncocare/anthropic_api_key --type SecureString --value "$KEY" >/dev/null
  fi
fi

# ---------------------------------------------------------------- IAM role + instance profile
ROLE="$NAME-ec2"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  say "Creating IAM role $ROLE"
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
aws iam attach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam put-role-policy --role-name "$ROLE" --policy-name oncocare-app --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
  {\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/*\"},
  {\"Effect\":\"Allow\",\"Action\":[\"ssm:GetParameter\",\"ssm:GetParameters\"],\"Resource\":\"arn:aws:ssm:$REGION:$ACCOUNT:parameter/oncocare/*\"}]}"
if ! aws iam get-instance-profile --instance-profile-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$ROLE" >/dev/null
fi
if [ "$(aws iam get-instance-profile --instance-profile-name "$ROLE" --query 'length(InstanceProfile.Roles)' --output text)" = "0" ]; then
  aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
fi

# ---------------------------------------------------------------- network: security group + Elastic IP
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
[ "$VPC" != "None" ] || die "No default VPC in $REGION. Create one with: aws ec2 create-default-vpc"
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values="$NAME-web" Name=vpc-id,Values="$VPC" --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG" = "None" ]; then
  say "Creating security group $NAME-web (HTTP/HTTPS only)"
  SG=$(aws ec2 create-security-group --group-name "$NAME-web" --description "OncoCare web (80/443)" --vpc-id "$VPC" --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
    'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}],Ipv6Ranges=[{CidrIpv6=::/0}]' \
    'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}],Ipv6Ranges=[{CidrIpv6=::/0}]' >/dev/null
fi
ALLOC=$(aws ec2 describe-addresses --filters Name=tag:Name,Values="$NAME" --query 'Addresses[0].AllocationId' --output text)
if [ "$ALLOC" = "None" ]; then
  say "Allocating Elastic IP"
  ALLOC=$(aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME}]" --query AllocationId --output text)
fi
EIP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text)

# ---------------------------------------------------------------- DNS: onco.thravi.in -> Elastic IP
ZONE=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME." --query "HostedZones[?Name=='$ZONE_NAME.' && Config.PrivateZone==\`false\`].Id | [0]" --output text)
[ "$ZONE" != "None" ] || die "Public hosted zone $ZONE_NAME not found in Route 53."
say "Route 53: $DOMAIN -> $EIP"
aws route53 change-resource-record-sets --hosted-zone-id "${ZONE##*/}" --change-batch "{\"Comment\":\"OncoCare\",\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"$DOMAIN\",\"Type\":\"A\",\"TTL\":300,\"ResourceRecords\":[{\"Value\":\"$EIP\"}]}}]}" >/dev/null

# ---------------------------------------------------------------- instance
INSTANCE=$(aws ec2 describe-instances --filters Name=tag:Name,Values="$NAME" Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
if [ "$INSTANCE" = "None" ]; then
  AMI=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
  say "Launching $INSTANCE_TYPE Ubuntu 24.04 ($AMI)"
  for attempt in 1 2 3 4 5 6 7 8; do
    if INSTANCE=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" --security-group-ids "$SG" \
        --iam-instance-profile Name="$ROLE" --metadata-options HttpTokens=required,HttpEndpoint=enabled \
        --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true,DeleteOnTermination=false}' \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" "ResourceType=volume,Tags=[{Key=Name,Value=$NAME}]" \
        --query 'Instances[0].InstanceId' --output text 2>/tmp/oncocare-run.err); then break; fi
    grep -q "instance profile" /tmp/oncocare-run.err || { cat /tmp/oncocare-run.err; exit 1; }
    echo "   waiting for the new IAM role to propagate… ($attempt)"; sleep 10
  done
  [ -n "$INSTANCE" ] && [ "$INSTANCE" != "None" ] || die "Could not launch the instance."
fi
if [ "$(aws ec2 describe-instances --instance-ids "$INSTANCE" --query 'Reservations[0].Instances[0].State.Name' --output text)" = "stopped" ]; then
  aws ec2 start-instances --instance-ids "$INSTANCE" >/dev/null
fi
say "Waiting for instance $INSTANCE to run"
aws ec2 wait instance-running --instance-ids "$INSTANCE"
aws ec2 associate-address --instance-id "$INSTANCE" --allocation-id "$ALLOC" --allow-reassociation >/dev/null

say "Waiting for the instance to register with SSM (first launch takes 1-3 minutes)"
for i in $(seq 1 60); do
  [ "$(aws ssm describe-instance-information --filters Key=InstanceIds,Values="$INSTANCE" --query 'InstanceInformationList[0].PingStatus' --output text)" = "Online" ] && break
  [ "$i" = 60 ] && die "Instance did not come online in SSM."
  sleep 5
done

# ---------------------------------------------------------------- package + ship the release
SHA=$(git rev-parse --short HEAD)
[ -z "$(git status --porcelain --untracked-files=no)" ] || echo "   note: uncommitted changes are NOT deployed (deploying commit $SHA)"
REL="$(date -u +%Y%m%d%H%M%S)-$SHA"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git archive --format=tar.gz -o "$WORK/release.tgz" HEAD
aws s3 cp --quiet "$WORK/release.tgz" "s3://$BUCKET/releases/$REL.tgz"

say "Installing release $REL on the instance"
cat > "$WORK/boot.sh" <<EOF
set -euo pipefail
command -v /snap/bin/aws >/dev/null || snap install aws-cli --classic >/dev/null
mkdir -p /opt/oncocare/releases/$REL
/snap/bin/aws s3 cp --quiet --region $REGION s3://$BUCKET/releases/$REL.tgz /tmp/oncocare-release.tgz
tar xzf /tmp/oncocare-release.tgz -C /opt/oncocare/releases/$REL && rm -f /tmp/oncocare-release.tgz
DOMAIN=$DOMAIN AWS_REGION=$REGION bash /opt/oncocare/releases/$REL/deploy/aws/remote-install.sh /opt/oncocare/releases/$REL
EOF
BOOT=$(base64 < "$WORK/boot.sh" | tr -d '\n')
CMD=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript --comment "OncoCare deploy $REL" \
  --parameters "{\"commands\":[\"echo $BOOT | base64 -d > /root/oncocare-deploy.sh && bash /root/oncocare-deploy.sh\"],\"executionTimeout\":[\"1200\"]}" \
  --query Command.CommandId --output text)
STATUS=Pending
while [ "$STATUS" = "Pending" ] || [ "$STATUS" = "InProgress" ] || [ "$STATUS" = "Delayed" ]; do
  sleep 5
  STATUS=$(aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)
done
aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" --query StandardOutputContent --output text | sed 's/^/   /'
if [ "$STATUS" != "Success" ]; then
  aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" --query StandardErrorContent --output text | tail -30
  die "Deploy command finished with status $STATUS."
fi

say "Checking https://$DOMAIN (the HTTPS certificate can take a minute on first deploy)"
for i in $(seq 1 30); do
  if curl -fsS -o /dev/null --max-time 10 "https://$DOMAIN/health" 2>/dev/null; then OK=1; break; fi
  sleep 10
done
[ "${OK:-}" = 1 ] && say "Live: https://$DOMAIN" || echo "   Not reachable over HTTPS yet - DNS may still be propagating. Try https://$DOMAIN in a few minutes."
echo
echo "   Demo sign-in password (clinician and patient portal):"
echo "     $(aws ssm get-parameter --name /oncocare/demo_password --with-decryption --query Parameter.Value --output text)"
echo "   Clinician sign-in: radiodoc / chemodoc / surgerydoc / staff, password qwertyui"
echo "   Server shell (needs session-manager-plugin): aws ssm start-session --region $REGION --target $INSTANCE"
