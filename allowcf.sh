#!/bin/bash

# 脚本说明: 限制本地端口443和80只允许Cloudflare IP访问
# 使用方法: sudo ./allowcf.sh

# 不使用 set -e，手动处理错误
set +e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查是否为root用户
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}错误: 请使用sudo运行此脚本${NC}"
    exit 1
fi

# 检测防火墙类型
detect_firewall() {
    if command -v iptables &> /dev/null && iptables -L &> /dev/null; then
        echo "iptables"
    elif command -v firewall-cmd &> /dev/null && firewall-cmd --state &> /dev/null; then
        echo "firewalld"
    else
        echo "unknown"
    fi
}

# 获取Cloudflare IP地址范围
get_cloudflare_ips() {
    # 所有信息输出到stderr，只有路径输出到stdout
    echo -e "${YELLOW}正在获取Cloudflare IP地址范围...${NC}" >&2
    
    # 检查curl是否可用
    if ! command -v curl &> /dev/null; then
        echo -e "${RED}✗ 错误: 未找到curl命令，请先安装curl${NC}" >&2
        echo -e "${YELLOW}  安装方法: apt-get install curl 或 yum install curl${NC}" >&2
        exit 1
    fi
    
    # Cloudflare IPv4地址范围
    CF_IPV4_URL="https://www.cloudflare.com/ips-v4"
    # Cloudflare IPv6地址范围
    CF_IPV6_URL="https://www.cloudflare.com/ips-v6"
    
    # 创建临时文件
    TEMP_DIR=$(mktemp -d)
    if [ $? -ne 0 ]; then
        echo -e "${RED}✗ 错误: 无法创建临时目录${NC}" >&2
        exit 1
    fi
    
    CF_IPV4_FILE="$TEMP_DIR/cf-ipv4.txt"
    CF_IPV6_FILE="$TEMP_DIR/cf-ipv6.txt"
    
    # 下载IP列表（添加超时和重试）
    echo -e "${YELLOW}  正在下载IPv4地址列表...${NC}" >&2
    local retry_count=0
    local max_retries=3
    local download_success=0
    
    while [ $retry_count -lt $max_retries ]; do
        if curl -s -f --connect-timeout 10 --max-time 30 "$CF_IPV4_URL" -o "$CF_IPV4_FILE" 2>&1; then
            if [ -s "$CF_IPV4_FILE" ]; then
                download_success=1
                break
            fi
        fi
        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo -e "${YELLOW}  重试中 ($retry_count/$max_retries)...${NC}" >&2
            sleep 2
        fi
    done
    
    if [ $download_success -eq 1 ]; then
        local ip_count=$(wc -l < "$CF_IPV4_FILE" | tr -d ' ')
        echo -e "${GREEN}✓ 成功获取Cloudflare IPv4地址范围 ($ip_count 个IP段)${NC}" >&2
    else
        echo -e "${RED}✗ 无法获取Cloudflare IPv4地址范围（已重试 $max_retries 次）${NC}" >&2
        echo -e "${YELLOW}  请检查网络连接或稍后重试${NC}" >&2
        rm -rf "$TEMP_DIR"
        exit 1
    fi
    
    # 下载IPv6列表
    echo -e "${YELLOW}  正在下载IPv6地址列表...${NC}" >&2
    retry_count=0
    download_success=0
    
    while [ $retry_count -lt $max_retries ]; do
        if curl -s -f --connect-timeout 10 --max-time 30 "$CF_IPV6_URL" -o "$CF_IPV6_FILE" 2>&1; then
            if [ -s "$CF_IPV6_FILE" ]; then
                download_success=1
                break
            fi
        fi
        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 2
        fi
    done
    
    if [ $download_success -eq 1 ]; then
        local ip6_count=$(wc -l < "$CF_IPV6_FILE" | tr -d ' ')
        echo -e "${GREEN}✓ 成功获取Cloudflare IPv6地址范围 ($ip6_count 个IP段)${NC}" >&2
    else
        echo -e "${YELLOW}⚠ 无法获取Cloudflare IPv6地址范围（可能不需要）${NC}" >&2
        touch "$CF_IPV6_FILE"
    fi
    
    echo "$TEMP_DIR"
}

# 删除iptables中允许所有IP访问80/443的规则
remove_existing_iptables_rules() {
    echo -e "${YELLOW}检查并删除现有的80/443端口开放规则...${NC}"
    
    local removed=0
    
    # 使用规则匹配删除，这是最安全的方法
    # 删除允许所有IP访问80端口的规则（IPv4）
    while iptables -D INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null; do
        echo -e "${YELLOW}  删除规则: 允许所有IP访问80端口 (IPv4)${NC}"
        removed=$((removed + 1))
    done
    
    # 删除允许所有IP访问443端口的规则（IPv4）
    while iptables -D INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null; do
        echo -e "${YELLOW}  删除规则: 允许所有IP访问443端口 (IPv4)${NC}"
        removed=$((removed + 1))
    done
    
    # IPv6规则
    while ip6tables -D INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null; do
        echo -e "${YELLOW}  删除规则: 允许所有IP访问80端口 (IPv6)${NC}"
        removed=$((removed + 1))
    done
    
    while ip6tables -D INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null; do
        echo -e "${YELLOW}  删除规则: 允许所有IP访问443端口 (IPv6)${NC}"
        removed=$((removed + 1))
    done
    
    # 也检查并删除可能存在的其他形式的规则（通过行号）
    # 只删除一次，避免重复删除
    local max_attempts=5
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        local found_any=0
        
        # 获取所有80/443端口的规则行号（倒序）
        local line_numbers=$(iptables -L INPUT --line-numbers -n 2>/dev/null | \
            awk '/tcp.*dpt:(80|443)/ && /ACCEPT/ && !/CLOUDFLARE/ {print $1}' | \
            sort -rn | head -1)
        
        if [ -n "$line_numbers" ]; then
            for line_num in $line_numbers; do
                # 获取规则内容
                local rule_content=$(iptables -L INPUT -n --line-numbers 2>/dev/null | \
                    awk -v num="$line_num" 'NR==num {for(i=2;i<=NF;i++) printf $i" "; print ""}')
                
                # 检查是否没有source限制（允许所有IP）
                if echo "$rule_content" | grep -qE "ACCEPT.*tcp.*dpt:(80|443)" && \
                   ! echo "$rule_content" | grep -qE "source|src"; then
                    echo -e "${YELLOW}  删除规则 #${line_num}: $rule_content${NC}"
                    iptables -D INPUT "$line_num" 2>/dev/null && removed=$((removed + 1)) && found_any=1 || true
                    break
                fi
            done
        fi
        
        if [ $found_any -eq 0 ]; then
            break
        fi
        
        attempt=$((attempt + 1))
    done
    
    if [ $removed -gt 0 ]; then
        echo -e "${GREEN}✓ 已删除 $removed 条现有规则${NC}"
    else
        echo -e "${YELLOW}  未发现需要删除的规则${NC}"
    fi
}

# 使用iptables设置规则
setup_iptables() {
    local temp_dir=$1
    local cf_ipv4_file="$temp_dir/cf-ipv4.txt"
    local cf_ipv6_file="$temp_dir/cf-ipv6.txt"
    
    echo -e "${YELLOW}使用iptables设置防火墙规则...${NC}"
    
    # 验证文件是否存在
    if [ ! -f "$cf_ipv4_file" ] || [ ! -s "$cf_ipv4_file" ]; then
        echo -e "${RED}✗ 错误: Cloudflare IPv4文件不存在或为空: $cf_ipv4_file${NC}" >&2
        return 1
    fi
    
    # 先删除现有的开放规则
    remove_existing_iptables_rules
    
    # 创建自定义链（如果不存在）
    iptables -N CLOUDFLARE_HTTP 2>/dev/null || true
    iptables -N CLOUDFLARE_HTTPS 2>/dev/null || true
    ip6tables -N CLOUDFLARE_HTTP 2>/dev/null || true
    ip6tables -N CLOUDFLARE_HTTPS 2>/dev/null || true
    
    # 清空现有规则
    iptables -F CLOUDFLARE_HTTP
    iptables -F CLOUDFLARE_HTTPS
    ip6tables -F CLOUDFLARE_HTTP 2>/dev/null || true
    ip6tables -F CLOUDFLARE_HTTPS 2>/dev/null || true
    
    # 添加Cloudflare IPv4规则到HTTP链
    echo -e "${YELLOW}添加Cloudflare IPv4规则到HTTP (80端口)...${NC}"
    local ip_count=0
    while IFS= read -r ip || [ -n "$ip" ]; do
        # 去除前后空白字符
        ip=$(echo "$ip" | xargs)
        [ -z "$ip" ] && continue
        # 验证IP格式（简单检查）
        if [[ "$ip" =~ ^[0-9./]+$ ]] || [[ "$ip" =~ ^[0-9a-fA-F:./]+$ ]]; then
            if iptables -A CLOUDFLARE_HTTP -s "$ip" -j ACCEPT 2>/dev/null; then
                ip_count=$((ip_count + 1))
            fi
        fi
    done < "$cf_ipv4_file"
    echo -e "${GREEN}  已添加 $ip_count 条IPv4规则到HTTP链${NC}"
    
    # 添加Cloudflare IPv4规则到HTTPS链
    echo -e "${YELLOW}添加Cloudflare IPv4规则到HTTPS (443端口)...${NC}"
    ip_count=0
    while IFS= read -r ip || [ -n "$ip" ]; do
        # 去除前后空白字符
        ip=$(echo "$ip" | xargs)
        [ -z "$ip" ] && continue
        # 验证IP格式（简单检查）
        if [[ "$ip" =~ ^[0-9./]+$ ]] || [[ "$ip" =~ ^[0-9a-fA-F:./]+$ ]]; then
            if iptables -A CLOUDFLARE_HTTPS -s "$ip" -j ACCEPT 2>/dev/null; then
                ip_count=$((ip_count + 1))
            fi
        fi
    done < "$cf_ipv4_file"
    echo -e "${GREEN}  已添加 $ip_count 条IPv4规则到HTTPS链${NC}"
    
    # 添加Cloudflare IPv6规则（如果存在）
    if [ -s "$cf_ipv6_file" ]; then
        echo -e "${YELLOW}添加Cloudflare IPv6规则...${NC}"
        local ip6_count=0
        while IFS= read -r ip || [ -n "$ip" ]; do
            # 去除前后空白字符
            ip=$(echo "$ip" | xargs)
            [ -z "$ip" ] && continue
            # 验证IPv6格式（简单检查）
            if [[ "$ip" =~ ^[0-9a-fA-F:./]+$ ]]; then
                if ip6tables -A CLOUDFLARE_HTTP -s "$ip" -j ACCEPT 2>/dev/null; then
                    ip6_count=$((ip6_count + 1))
                fi
                ip6tables -A CLOUDFLARE_HTTPS -s "$ip" -j ACCEPT 2>/dev/null || true
            fi
        done < "$cf_ipv6_file"
        echo -e "${GREEN}  已添加 $ip6_count 条IPv6规则${NC}"
    fi
    
    # 拒绝所有其他IP
    iptables -A CLOUDFLARE_HTTP -j DROP
    iptables -A CLOUDFLARE_HTTPS -j DROP
    ip6tables -A CLOUDFLARE_HTTP -j DROP 2>/dev/null || true
    ip6tables -A CLOUDFLARE_HTTPS -j DROP 2>/dev/null || true
    
    # 将规则应用到INPUT链
    # 移除旧规则（如果存在）- 需要删除所有可能的匹配规则
    echo -e "${YELLOW}清理旧的规则...${NC}"
    while iptables -D INPUT -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; do :; done
    while iptables -D INPUT -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; do :; done
    while iptables -D INPUT ! -i lo -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; do :; done
    while iptables -D INPUT ! -i lo -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; do :; done
    
    # 添加新规则到INPUT链（使用-I插入到链的开头，优先匹配）
    # 匹配所有接口的所有流量（包括本地回环，如果需要限制本地访问可以后续添加规则）
    # 直接匹配所有流量，确保规则生效
    echo -e "${YELLOW}添加规则到INPUT链...${NC}"
    if ! iptables -I INPUT 1 -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; then
        echo -e "${RED}✗ 错误: 无法添加80端口规则到INPUT链${NC}" >&2
        return 1
    fi
    
    if ! iptables -I INPUT 1 -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; then
        echo -e "${RED}✗ 错误: 无法添加443端口规则到INPUT链${NC}" >&2
        return 1
    fi
    
    # 添加允许本地回环访问的规则（在CLOUDFLARE规则之前）
    # 这样本地可以访问，但外部非Cloudflare IP会被拒绝
    iptables -I INPUT 1 -i lo -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
    iptables -I INPUT 1 -i lo -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
    
    # IPv6规则
    while ip6tables -D INPUT -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; do :; done
    while ip6tables -D INPUT -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; do :; done
    ip6tables -I INPUT 1 -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null || true
    ip6tables -I INPUT 1 -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null || true
    ip6tables -I INPUT 1 -i lo -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
    ip6tables -I INPUT 1 -i lo -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
    
    # 检测Docker并添加FORWARD链规则
    echo ""
    echo -e "${YELLOW}检测Docker环境...${NC}"
    if iptables -L DOCKER-USER &>/dev/null; then
        echo -e "${GREEN}  检测到Docker，添加规则到DOCKER-USER链（推荐方式）${NC}"
        
        # 清理DOCKER-USER链中的旧规则（IPv4）
        while iptables -D DOCKER-USER -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; do :; done
        while iptables -D DOCKER-USER -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; do :; done
        
        # 在DOCKER-USER链开头添加规则（Docker推荐的方式）
        # DOCKER-USER链在Docker规则之前处理，所以我们的规则会优先
        iptables -I DOCKER-USER 1 -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null || true
        iptables -I DOCKER-USER 1 -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null || true
        
        # 添加IPv6规则到DOCKER-USER链（如果Docker支持IPv6）
        # 注意：Docker的IPv6支持可能有限，但尝试添加
        if ip6tables -L DOCKER-USER &>/dev/null 2>&1; then
            while ip6tables -D DOCKER-USER -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; do :; done
            while ip6tables -D DOCKER-USER -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; do :; done
            ip6tables -I DOCKER-USER 1 -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null || true
            ip6tables -I DOCKER-USER 1 -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null || true
            echo -e "${GREEN}  ✓ 已添加IPv6规则到DOCKER-USER链${NC}"
        else
            echo -e "${YELLOW}  Docker IPv6 DOCKER-USER链不可用，跳过IPv6规则${NC}"
        fi
        
        echo -e "${GREEN}  ✓ 已添加IPv4规则到DOCKER-USER链${NC}"
    else
        echo -e "${YELLOW}  未检测到DOCKER-USER链，尝试在FORWARD链中添加规则${NC}"
        
        # 清理FORWARD链中的旧规则（IPv4）
        while iptables -D FORWARD -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; do :; done
        while iptables -D FORWARD -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; do :; done
        
        # 在FORWARD链开头添加规则
        # 注意：需要在Docker规则之前，所以使用-I插入到开头
        iptables -I FORWARD 1 -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null || true
        iptables -I FORWARD 1 -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null || true
        
        # 添加IPv6规则到FORWARD链
        while ip6tables -D FORWARD -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null; do :; done
        while ip6tables -D FORWARD -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null; do :; done
        ip6tables -I FORWARD 1 -p tcp --dport 80 -j CLOUDFLARE_HTTP 2>/dev/null || true
        ip6tables -I FORWARD 1 -p tcp --dport 443 -j CLOUDFLARE_HTTPS 2>/dev/null || true
        
        echo -e "${GREEN}  ✓ 已添加规则到FORWARD链（IPv4和IPv6）${NC}"
    fi
    
    echo -e "${GREEN}✓ iptables规则设置完成${NC}"
    
    # 显示规则验证信息
    echo -e "${YELLOW}验证规则...${NC}"
    local http_rules=$(iptables -L INPUT -n --line-numbers | grep -c "CLOUDFLARE_HTTP" || echo "0")
    local https_rules=$(iptables -L INPUT -n --line-numbers | grep -c "CLOUDFLARE_HTTPS" || echo "0")
    echo -e "${GREEN}  INPUT链中HTTP规则数: $http_rules${NC}"
    echo -e "${GREEN}  INPUT链中HTTPS规则数: $https_rules${NC}"
    
    # 检查IPv6规则
    echo ""
    echo -e "${YELLOW}检查IPv6规则...${NC}"
    local ipv6_http=$(ip6tables -L INPUT -n --line-numbers 2>/dev/null | grep -c "CLOUDFLARE_HTTP" || echo "0")
    local ipv6_https=$(ip6tables -L INPUT -n --line-numbers 2>/dev/null | grep -c "CLOUDFLARE_HTTPS" || echo "0")
    echo -e "${GREEN}  IPv6 INPUT链中HTTP规则数: $ipv6_http${NC}"
    echo -e "${GREEN}  IPv6 INPUT链中HTTPS规则数: $ipv6_https${NC}"
    
    # 检查DOCKER-USER或FORWARD链
    if iptables -L DOCKER-USER &>/dev/null; then
        local docker_http=$(iptables -L DOCKER-USER -n --line-numbers | grep -c "CLOUDFLARE_HTTP" || echo "0")
        local docker_https=$(iptables -L DOCKER-USER -n --line-numbers | grep -c "CLOUDFLARE_HTTPS" || echo "0")
        echo -e "${GREEN}  DOCKER-USER链中HTTP规则数: $docker_http${NC}"
        echo -e "${GREEN}  DOCKER-USER链中HTTPS规则数: $docker_https${NC}"
        
        # 检查IPv6 DOCKER-USER链
        if ip6tables -L DOCKER-USER &>/dev/null 2>&1; then
            local ipv6_docker_http=$(ip6tables -L DOCKER-USER -n --line-numbers 2>/dev/null | grep -c "CLOUDFLARE_HTTP" || echo "0")
            local ipv6_docker_https=$(ip6tables -L DOCKER-USER -n --line-numbers 2>/dev/null | grep -c "CLOUDFLARE_HTTPS" || echo "0")
            echo -e "${GREEN}  IPv6 DOCKER-USER链中HTTP规则数: $ipv6_docker_http${NC}"
            echo -e "${GREEN}  IPv6 DOCKER-USER链中HTTPS规则数: $ipv6_docker_https${NC}"
        fi
    else
        local forward_http=$(iptables -L FORWARD -n --line-numbers | grep -c "CLOUDFLARE_HTTP" || echo "0")
        local forward_https=$(iptables -L FORWARD -n --line-numbers | grep -c "CLOUDFLARE_HTTPS" || echo "0")
        echo -e "${GREEN}  FORWARD链中HTTP规则数: $forward_http${NC}"
        echo -e "${GREEN}  FORWARD链中HTTPS规则数: $forward_https${NC}"
        
        # 检查IPv6 FORWARD链
        local ipv6_forward_http=$(ip6tables -L FORWARD -n --line-numbers 2>/dev/null | grep -c "CLOUDFLARE_HTTP" || echo "0")
        local ipv6_forward_https=$(ip6tables -L FORWARD -n --line-numbers 2>/dev/null | grep -c "CLOUDFLARE_HTTPS" || echo "0")
        echo -e "${GREEN}  IPv6 FORWARD链中HTTP规则数: $ipv6_forward_http${NC}"
        echo -e "${GREEN}  IPv6 FORWARD链中HTTPS规则数: $ipv6_forward_https${NC}"
    fi
    
    # 显示规则位置
    local http_pos=$(iptables -L INPUT -n --line-numbers | grep "CLOUDFLARE_HTTP" | head -1 | awk '{print $1}' || echo "未找到")
    local https_pos=$(iptables -L INPUT -n --line-numbers | grep "CLOUDFLARE_HTTPS" | head -1 | awk '{print $1}' || echo "未找到")
    echo -e "${GREEN}  HTTP规则位置: 第 $http_pos 条${NC}"
    echo -e "${GREEN}  HTTPS规则位置: 第 $https_pos 条${NC}"
    
    # 检查是否有其他规则在CLOUDFLARE规则之前
    echo ""
    echo -e "${YELLOW}检查INPUT链规则顺序...${NC}"
    local all_rules=$(iptables -L INPUT -n --line-numbers | head -10)
    echo "$all_rules"
    
    # 检查CLOUDFLARE链中的规则数量
    local cf_http_count=$(iptables -L CLOUDFLARE_HTTP -n | grep -c "ACCEPT" || echo "0")
    local cf_https_count=$(iptables -L CLOUDFLARE_HTTPS -n | grep -c "ACCEPT" || echo "0")
    echo -e "${GREEN}  CLOUDFLARE_HTTP链中ACCEPT规则数: $cf_http_count${NC}"
    echo -e "${GREEN}  CLOUDFLARE_HTTPS链中ACCEPT规则数: $cf_https_count${NC}"
    
    # 检查是否有DROP规则
    local cf_http_drop=$(iptables -L CLOUDFLARE_HTTP -n | grep -c "DROP" || echo "0")
    local cf_https_drop=$(iptables -L CLOUDFLARE_HTTPS -n | grep -c "DROP" || echo "0")
    echo -e "${GREEN}  CLOUDFLARE_HTTP链中DROP规则数: $cf_http_drop${NC}"
    echo -e "${GREEN}  CLOUDFLARE_HTTPS链中DROP规则数: $cf_https_drop${NC}"
    
    # 检查服务监听位置
    echo ""
    echo -e "${YELLOW}检查服务监听位置...${NC}"
    if command -v netstat &> /dev/null; then
        local http_listen=$(netstat -tlnp 2>/dev/null | grep ":80 " || echo "")
        local https_listen=$(netstat -tlnp 2>/dev/null | grep ":443 " || echo "")
        if [ -n "$http_listen" ]; then
            echo -e "${YELLOW}  80端口监听:${NC}"
            echo "$http_listen" | while read -r line; do
                echo -e "    $line"
            done
        else
            echo -e "${YELLOW}  80端口未监听${NC}"
        fi
        if [ -n "$https_listen" ]; then
            echo -e "${YELLOW}  443端口监听:${NC}"
            echo "$https_listen" | while read -r line; do
                echo -e "    $line"
            done
        else
            echo -e "${YELLOW}  443端口未监听${NC}"
        fi
    elif command -v ss &> /dev/null; then
        local http_listen=$(ss -tlnp 2>/dev/null | grep ":80 " || echo "")
        local https_listen=$(ss -tlnp 2>/dev/null | grep ":443 " || echo "")
        if [ -n "$http_listen" ]; then
            echo -e "${YELLOW}  80端口监听:${NC}"
            echo "$http_listen" | while read -r line; do
                echo -e "    $line"
            done
        else
            echo -e "${YELLOW}  80端口未监听${NC}"
        fi
        if [ -n "$https_listen" ]; then
            echo -e "${YELLOW}  443端口监听:${NC}"
            echo "$https_listen" | while read -r line; do
                echo -e "    $line"
            done
        else
            echo -e "${YELLOW}  443端口未监听${NC}"
        fi
    fi
    
    # 检查Docker网络
    echo ""
    echo -e "${YELLOW}检查Docker网络规则...${NC}"
    local docker_443=$(iptables -L DOCKER -n | grep "dpt:443" || echo "")
    if [ -n "$docker_443" ]; then
        echo -e "${YELLOW}  警告: 发现Docker网络中有443端口规则，如果服务在Docker容器中，可能需要额外配置${NC}"
        echo "$docker_443" | while read -r line; do
            echo -e "    $line"
        done
    fi
    
    return 0
}

# 删除firewalld中允许所有IP访问80/443的规则
remove_existing_firewalld_rules() {
    echo -e "${YELLOW}检查并删除现有的80/443端口开放规则...${NC}"
    
    local removed=0
    
    # 检查并移除开放的http/https服务
    if firewall-cmd --permanent --query-service=http 2>/dev/null; then
        echo -e "${YELLOW}  移除开放的http服务...${NC}"
        firewall-cmd --permanent --remove-service=http 2>/dev/null && removed=$((removed + 1)) || true
    fi
    
    if firewall-cmd --permanent --query-service=https 2>/dev/null; then
        echo -e "${YELLOW}  移除开放的https服务...${NC}"
        firewall-cmd --permanent --remove-service=https 2>/dev/null && removed=$((removed + 1)) || true
    fi
    
    # 检查并移除直接开放的80/443端口
    if firewall-cmd --permanent --query-port=80/tcp 2>/dev/null; then
        echo -e "${YELLOW}  移除开放的80/tcp端口...${NC}"
        firewall-cmd --permanent --remove-port=80/tcp 2>/dev/null && removed=$((removed + 1)) || true
    fi
    
    if firewall-cmd --permanent --query-port=443/tcp 2>/dev/null; then
        echo -e "${YELLOW}  移除开放的443/tcp端口...${NC}"
        firewall-cmd --permanent --remove-port=443/tcp 2>/dev/null && removed=$((removed + 1)) || true
    fi
    
    # 移除允许所有IP访问80/443的富规则
    local rich_rules=$(firewall-cmd --permanent --list-rich-rules 2>/dev/null | grep -E "port port='(80|443)'" || true)
    if [ -n "$rich_rules" ]; then
        while IFS= read -r rule; do
            # 只删除没有source限制的accept规则
            if echo "$rule" | grep -q "accept" && ! echo "$rule" | grep -q "source address"; then
                echo -e "${YELLOW}  删除富规则: $rule${NC}"
                firewall-cmd --permanent --remove-rich-rule="$rule" 2>/dev/null && removed=$((removed + 1)) || true
            fi
        done <<< "$rich_rules"
    fi
    
    if [ $removed -gt 0 ]; then
        echo -e "${GREEN}✓ 已删除 $removed 条现有规则/服务${NC}"
        firewall-cmd --reload 2>/dev/null || true
    else
        echo -e "${YELLOW}  未发现需要删除的规则${NC}"
    fi
}

# 使用firewalld设置规则
setup_firewalld() {
    local temp_dir=$1
    local cf_ipv4_file="$temp_dir/cf-ipv4.txt"
    local cf_ipv6_file="$temp_dir/cf-ipv6.txt"
    
    echo -e "${YELLOW}使用firewalld设置防火墙规则...${NC}"
    
    # 先删除现有的开放规则
    remove_existing_firewalld_rules
    
    # 创建富规则（rich rules）
    # 首先移除旧规则（如果存在）
    firewall-cmd --permanent --remove-rich-rule="rule family='ipv4' port port='80' protocol='tcp' reject" 2>/dev/null || true
    firewall-cmd --permanent --remove-rich-rule="rule family='ipv4' port port='443' protocol='tcp' reject" 2>/dev/null || true
    
    # 添加Cloudflare IPv4规则
    echo -e "${YELLOW}添加Cloudflare IPv4规则...${NC}"
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
        firewall-cmd --permanent --add-rich-rule="rule family='ipv4' source address='$ip' port port='80' protocol='tcp' accept"
        firewall-cmd --permanent --add-rich-rule="rule family='ipv4' source address='$ip' port port='443' protocol='tcp' accept"
    done < "$cf_ipv4_file"
    
    # 添加Cloudflare IPv6规则（如果存在）
    if [ -s "$cf_ipv6_file" ]; then
        echo -e "${YELLOW}添加Cloudflare IPv6规则...${NC}"
        while IFS= read -r ip; do
            [ -z "$ip" ] && continue
            firewall-cmd --permanent --add-rich-rule="rule family='ipv6' source address='$ip' port port='80' protocol='tcp' accept" 2>/dev/null || true
            firewall-cmd --permanent --add-rich-rule="rule family='ipv6' source address='$ip' port port='443' protocol='tcp' accept" 2>/dev/null || true
        done < "$cf_ipv6_file"
    fi
    
    # 拒绝所有其他IP访问80和443端口
    firewall-cmd --permanent --add-rich-rule="rule family='ipv4' port port='80' protocol='tcp' reject"
    firewall-cmd --permanent --add-rich-rule="rule family='ipv4' port port='443' protocol='tcp' reject"
    
    # 重新加载firewalld
    if ! firewall-cmd --reload 2>/dev/null; then
        echo -e "${RED}✗ 错误: 无法重新加载firewalld${NC}"
        return 1
    fi
    
    echo -e "${GREEN}✓ firewalld规则设置完成${NC}"
    return 0
}

# 主函数
main() {
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}Cloudflare IP访问限制脚本${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    
    # 检测防火墙类型
    FIREWALL_TYPE=$(detect_firewall)
    echo -e "${YELLOW}检测到防火墙类型: $FIREWALL_TYPE${NC}"
    
    if [ "$FIREWALL_TYPE" = "unknown" ]; then
        echo -e "${RED}错误: 未检测到iptables或firewalld，请先安装其中一个${NC}"
        exit 1
    fi
    
    # 获取Cloudflare IP
    echo ""
    TEMP_DIR=$(get_cloudflare_ips)
    if [ $? -ne 0 ] || [ -z "$TEMP_DIR" ]; then
        echo -e "${RED}错误: 无法获取Cloudflare IP地址范围${NC}"
        exit 1
    fi
    
    # 根据防火墙类型设置规则
    echo ""
    if [ "$FIREWALL_TYPE" = "iptables" ]; then
        setup_iptables "$TEMP_DIR"
        if [ $? -ne 0 ]; then
            echo -e "${RED}错误: iptables规则设置失败${NC}"
            rm -rf "$TEMP_DIR"
            exit 1
        fi
    elif [ "$FIREWALL_TYPE" = "firewalld" ]; then
        setup_firewalld "$TEMP_DIR"
        if [ $? -ne 0 ]; then
            echo -e "${RED}错误: firewalld规则设置失败${NC}"
            rm -rf "$TEMP_DIR"
            exit 1
        fi
    fi
    
    # 清理临时文件
    rm -rf "$TEMP_DIR" 2>/dev/null || true
    
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}✓ 脚本执行完成！${NC}"
    echo -e "${GREEN}端口80和443现在只允许Cloudflare IP访问${NC}"
    echo -e "${YELLOW}注意: 请确保您的服务器在Cloudflare后面，否则将无法访问${NC}"
    echo ""
    
    # 显示诊断信息
    if [ "$FIREWALL_TYPE" = "iptables" ]; then
        echo -e "${YELLOW}诊断信息:${NC}"
        echo -e "${YELLOW}----------------------------------------${NC}"
        
        # 检查INPUT链默认策略
        local input_policy=$(iptables -L INPUT -n | head -1 | grep -oP 'policy \K\w+')
        echo -e "INPUT链默认策略: $input_policy"
        if [ "$input_policy" = "ACCEPT" ]; then
            echo -e "${YELLOW}  警告: INPUT链默认策略为ACCEPT，如果规则不匹配，流量将被接受${NC}"
            echo -e "${YELLOW}  建议: 确保规则在INPUT链的最前面${NC}"
        fi
        
        # 显示规则详情
        echo ""
        echo -e "当前INPUT链规则（前5条）:"
        iptables -L INPUT -n --line-numbers | head -6 | tail -5
        
        echo ""
        echo -e "CLOUDFLARE_HTTPS链规则数量:"
        iptables -L CLOUDFLARE_HTTPS -n | grep -c "ACCEPT" || echo "0"
        
        echo ""
        echo -e "${YELLOW}测试建议:${NC}"
        echo -e "1. 从非Cloudflare IP测试: curl -v http://您的服务器IP:80"
        echo -e "2. 从非Cloudflare IP测试: curl -v https://您的服务器IP:443"
        echo -e "3. 应该看到连接被拒绝或超时"
        echo -e "4. 通过Cloudflare域名访问应该正常"
    fi
    
    echo -e "${GREEN}========================================${NC}"
}

# 运行主函数
main

