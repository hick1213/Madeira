-- Exercise every parser-private fixed name and the independent break marker.
local sum = 0
for i = 1, 5 do sum = sum + i end
assert(sum == 15)
for i = 5, 1, -2 do sum = sum + i end
assert(sum == 24)

local closures = {}
for i = 1, 3 do closures[i] = function() return i end end
assert(closures[1]() == 1 and closures[3]() == 3)

local total = 0
for key, value in pairs({a = 2, b = 3}) do
  total = total + value
end
assert(total == 5)
local next = next
for key, value in next, {a = 4}, nil do total = total + value end
assert(total == 9)
local function upvalue_iterator()
  for key, value in next, {a = 7}, nil do total = total + value end
end
upvalue_iterator()
assert(total == 16)

local saved
for i = 1, 4 do
  do
    local captured = i
    saved = function() return captured end
    if i == 2 then break end
  end
end
assert(saved() == 2)

local count = 0
::again::
count = count + 1
if count < 3 then goto again end
goto done
count = 100
::done::
assert(count == 3)

-- Debug-name serialization must retain the actual fixed-name byte tags.
local seen = {}
for i = 1, 1 do
  for slot = 1, 100 do
    local name = debug.getlocal(1, slot)
    if not name then break end
    seen[name] = true
  end
end
assert(seen["(for index)"] and seen["(for limit)"] and seen["(for step)"])
for key, value in pairs({one = 1}) do
  for slot = 1, 100 do
    local name = debug.getlocal(1, slot)
    if not name then break end
    seen[name] = true
  end
end
assert(seen["(for generator)"] and seen["(for state)"] and seen["(for control)"])

local cases = {
  {"break", "no loop to break"},
  {"goto absent", "undefined label"},
  {"goto destination; local value; ::destination:: print(value)", "jumps into the scope"},
  {"::duplicate:: ::duplicate::", "duplicate label"},
}
for _, case in ipairs(cases) do
  local fn, err = loadstring(case[1])
  assert(not fn and err:find(case[2], 1, true), tostring(err))
end

collectgarbage("collect")
print("parser sentinels: numeric/generic loops, captured locals, break/goto, debug names, syntax errors PASS")
