-- Owned semantic corpus. Run through the public C API against both runtimes.
local checks = 0
local function equal(actual, expected)
  checks = checks + 1
  assert(actual == expected, 'check ' .. checks .. ': ' .. tostring(actual) .. ' ~= ' .. tostring(expected))
end
local function phase(name) print('CORPUS_PHASE ' .. name) end
phase('calls')
equal(jit.status(), false)
local function pack(...) return {n=select('#', ...), ...} end
local function args(a, ...) return a, select('#', ...), ... end
local p = pack(args(11, 22, nil, 44))
equal(p.n, 5); equal(p[1], 11); equal(p[2], 3); equal(p[3], 22); equal(p[4], nil); equal(p[5], 44)
local function tail(n, acc) if n == 0 then return acc end return tail(n-1, acc+n) end
equal(tail(20000, 0), 200010000)
local many = {}; for i=1, 200 do many[i]=i end
local function sum(...) local r=0; for i=1,select('#',...) do r=r+select(i,...) end return r end
equal(sum(unpack(many)), 20100)
local function make_counter(n) return function(d) n=n+d; return n end end
local counter=make_counter(3); equal(counter(7),10); collectgarbage(); equal(counter(2),12)
local function nested(n) if n == 0 then return 1 end return 1+nested(n-1) end
equal(nested(300),301)

phase('tables-and-metamethods')
local a={}; for i=1,1200 do a['k'..i]=i*3; a[i]=i end
collectgarbage(); equal(a.k1200,3600); equal(a[1000],1000)
for i=1,600 do a['k'..i]=nil end
local count=0; for _ in pairs(a) do count=count+1 end; equal(count,1800)
local meta={__index=function(_,k) return 'missing:'..k end,
  __newindex=function(t,k,v) rawset(t,k,v*2) end,
  __call=function(t,x) return t.value+x end,
  __add=function(x,y) return x.value+y.value end,
  __lt=function(x,y) return x.value<y.value end,
  __eq=function(x,y) return x.value==y.value end,
  __concat=function(x,y) return tostring(x.value)..':'..tostring(y.value) end}
local x=setmetatable({value=7},meta); local y=setmetatable({value=9},meta)
equal(x.unknown,'missing:unknown'); x.new=6; equal(x.new,12); equal(x(5),12)
equal(x+y,16); equal(x<y,true); equal(x==setmetatable({value=7},meta),true); equal(x..y,'7:9')
local s=string.rep('a\0b',2000); equal(#s,6000); equal(s:sub(2998,3003),'a\0ba\0b')
equal(string.gsub('abc123def456','%d+',function(v) return '['..v..']' end),'abc[123]def[456]')
equal(string.format('%04d/%s/%.2f',9,'ok',1.25),'0009/ok/1.25')
equal(bit.bxor(0x1234,0xff),0x12cb)
local dumped=string.dump(function(v) return v*7 end)
equal(dumped:sub(1,4),'\27LJ\1'); equal(assert(loadstring(dumped))(6),42)

phase('coroutines-and-errors')
local co=coroutine.create(function(v)
  local u=counter(v); local resumed=coroutine.yield(u,'paused'); return counter(resumed),'done'
end)
local ok, v, marker=coroutine.resume(co,5); equal(ok,true); equal(v,17); equal(marker,'paused')
ok,v,marker=coroutine.resume(co,4); equal(ok,true); equal(v,21); equal(marker,'done'); equal(coroutine.status(co),'dead')
local bad=coroutine.create(function() error('owned-coroutine-error') end)
ok,v=coroutine.resume(bad); equal(ok,false); equal(v:find('owned-coroutine-error',1,true)~=nil,true)
ok,v=xpcall(function() error('owned-error') end,function(e) return 'handled:'..e end)
equal(ok,false); equal(v:find('handled:',1,true)==1,true)
local hooks=0; debug.sethook(function() hooks=hooks+1 end,'',13)
local total=0; for i=1,100 do total=total+i end; debug.sethook()
equal(total,5050); equal(hooks>0,true)
local closed=0
do
  for i=1,48 do local proxy=newproxy(true); getmetatable(proxy).__gc=function() closed=closed+1 end end
end
collectgarbage(); collectgarbage(); equal(closed,48)

phase('native-c-api')
equal(c_copy('abc\0def'),'abc\0def')
equal(c_reenter(function(v) return v*7 end,6),42)
ok,v=pcall(c_fail,'native-input'); equal(ok,false); equal(v:find('owned-native-error',1,true)~=nil,true)
equal(c_pointer()==c_pointer(),true)
local ptr=c_pointer(); local keyed={[ptr]='native-key'}; collectgarbage(); equal(keyed[ptr],'native-key')

phase('ffi')
local ffi=require('ffi')
ffi.cdef[[
  void *memcpy(void *dest, const void *src, size_t count);
  void *memmove(void *dest, const void *src, size_t count);
  int abs(int value);
  typedef int (*owned_compare)(const void *, const void *);
  void qsort(void *base, size_t count, size_t width, owned_compare compare);
]]
equal(ffi.C.abs(-42),42)
local bytes=ffi.new('char[16]'); ffi.C.memcpy(bytes,'abcdef',6); ffi.C.memmove(bytes+1,bytes,6)
equal(ffi.string(bytes,7),'aabcdef')
local values=ffi.new('int[8]',9,-2,3,0,17,4,1,-8)
local callback_count=0
local cmp=ffi.cast('owned_compare',function(lhs,rhs)
  callback_count=callback_count+1
  local l=ffi.cast('const int *',lhs)[0]; local r=ffi.cast('const int *',rhs)[0]
  return l<r and -1 or (l>r and 1 or 0)
end)
ffi.C.qsort(values,8,ffi.sizeof('int'),cmp); cmp:free()
equal(callback_count>0,true)
for i,want in ipairs({-8,-2,0,1,3,4,9,17}) do equal(values[i-1],want) end
local ffi_closed=0
do for i=1,24 do local item=ffi.gc(ffi.new('int[1]',i),function() ffi_closed=ffi_closed+1 end) end end
collectgarbage(); collectgarbage(); equal(ffi_closed,24)
phase('complete')
print('CORPUS_PASS checks='..checks)
return checks
