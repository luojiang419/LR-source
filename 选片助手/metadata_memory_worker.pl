# ExifTool 常驻内存编辑器：只接收图片字节并返回图片字节，绝不打开原片写入。
use strict;
use warnings;
BEGIN { push @Image::ExifTool::configFiles, shift @ARGV; }
use Image::ExifTool;
use Image::ExifTool::XMP;
use MIME::Base64 qw(decode_base64);
use Encode qw(decode encode FB_CROAK);
chdir $INC[0] or die 'Cannot leave data directory';
binmode STDIN; binmode STDOUT; $|=1;

sub read_exact {
    my ($length)=@_; my $result='';
    while (length($result)<$length) {
        my $n=read(STDIN,my $part,$length-length($result));
        die "Incomplete image input" unless defined($n) && $n>0;
        $result.=$part;
    }
    return $result;
}
# 与 ExifTool CLI 的 -G1 -s -json 保持相同数值/二进制表示；仅供内存校验。
sub json_value {
    my ($value,$quoted)=@_;
    return 'null' unless defined $value;
    if (ref $value eq 'ARRAY') { return '['.join(',',map {json_value($_,$quoted)} @$value).']'; }
    if (ref $value eq 'HASH') { return '{'.join(',',map {json_value($_,1).':'.json_value($$value{$_},/^(?:XMP-(?:fk|dc):|IFD0:XP(?:Subject|Keywords)$|XMP-xmp:Label$)/ ? 1 : 0)} sort keys %$value).'}'; }
    if (ref $value eq 'SCALAR') {
        $value=$$value =~ /^Binary data \d+ bytes$/ ? "($$value, use -b option to extract)" : '(Binary data '.length($$value).' bytes, use -b option to extract)';
    }
    die 'Unsupported metadata value' if ref $value;
    return lc($value) if !$quoted && $value =~ /^(true|false)$/i;
    return $value if !$quoted && $value =~ /^-?(\d|[1-9]\d{1,14})(\.\d{1,16})?(e[-+]?\d{1,3})?$/i;
    $value=encode('UTF-8',$value) if utf8::is_utf8($value);
    my %esc=('"'=>'"',"\t"=>'t',"\n"=>'n',"\r"=>'r','\\'=>'\\');
    $value =~ s/(["\t\n\r\\])/\\$esc{$1}/sg;
    $value =~ tr/\0//d;
    $value =~ s/([\x01-\x1f\x7f])/sprintf('\\u%04X',ord $1)/sge;
    Image::ExifTool::XMP::FixUTF8(\$value);
    return '"'.$value.'"';
}
while (defined(my $header=<STDIN>)) {
    last unless $header =~ /^(\d+) (\d+)\n$/;
    my ($count,$length)=($1,$2);
    last if $count>8192 || $length>536870912;
    my @args;
    for (1..$count) { my $line=<STDIN>; exit 1 unless defined $line; push @args,decode_base64($line); }
    my $input=read_exact($length); my $output='';
    my $ok=eval {
        my $tool=Image::ExifTool->new;
        $tool->Options(Charset=>'UTF8',PrintConv=>1);
        if (@args && $args[0] eq '__FRAMEKEEP_READ__') {
            shift @args;
            $tool->Options(Duplicates=>0,List=>1);
            my @found=@args;
            my $info=$tool->ImageInfo(\$input,\@found);
            die($tool->GetValue('Error')) if $tool->GetValue('Error');
            my %values=('SourceFile'=>'-');
            for my $tag (@found) {
                next unless defined $$info{$tag};
                next if $tag =~ /^(.*?) ?\(/ && defined $$info{$1} && $tool->GetGroup($tag,1) eq $tool->GetGroup($1,1);
                my $key=$tool->GetGroup($tag,1).':'.Image::ExifTool::GetTagName($tag);
                $values{$key}=$$info{$tag} unless exists $values{$key};
            }
            $output='['.json_value(\%values,0).']';
        } else {
        for my $raw (@args) {
            my $arg=decode('UTF-8',$raw,FB_CROAK);
            die "Unsupported metadata argument" unless $arg =~ /^-([\w:.-]+)=(.*)$/s;
            my ($tag,$value)=($1,$2);
            my ($changed,$error)=$tool->SetNewValue($tag,length($value)?$value:undef);
            die($error || "Unknown or unwritable tag: $tag") unless $changed;
        }
        my $written=$tool->WriteInfo(\$input,\$output);
        die($tool->GetValue('Error') || 'ExifTool memory edit failed') unless $written;
        }
        1;
    };
    if ($ok) { print 'OK '.length($output)."\n",$output; }
    else { my $error=encode('UTF-8',"$@"); print 'ERR '.length($error)."\n",$error; }
}
