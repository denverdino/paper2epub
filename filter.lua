local eq_labels = {}
local eq_count = 0

local AUTOREF_LOWER = {
    fig = "Fig.\u{00a0}",
    tab = "Table\u{00a0}",
    sec = "\u{00a7}",
    eq = "Eq.\u{00a0}",
}

local AUTOREF_UPPER = {
    fig = "Figure\u{00a0}",
    tab = "Table\u{00a0}",
    sec = "Section\u{00a0}",
    eq = "Equation\u{00a0}",
}

local function ref_kind(label)
    if label:match("^fig:") then return "fig" end
    if label:match("^tab:") then return "tab" end
    if label:match("^sec:") then return "sec" end
    if label:match("^eq:") then return "eq" end
    return nil
end

local fig_num = 0
local tab_num = 0

return {
    -- Pass 1: collect equation labels (pandoc doesn't number them)
    {
        Math = function(el)
            if el.mathtype == "DisplayMath" then
                for label in el.text:gmatch("\\label%{([^}]+)%}") do
                    eq_count = eq_count + 1
                    eq_labels[label] = eq_count
                end
            end
        end,
    },

    -- Pass 2: modify elements
    {
        Image = function(el)
            el.src = el.src:gsub("%.pdf$", ".png")
            return el
        end,

        Figure = function(el)
            fig_num = fig_num + 1
            if el.caption and el.caption.long and #el.caption.long > 0 then
                local block = el.caption.long[1]
                if block.content then
                    block.content:insert(1, pandoc.Space())
                    block.content:insert(1, pandoc.Strong{
                        pandoc.Str("Figure " .. fig_num .. ":")
                    })
                end
            end
            return el
        end,

        Table = function(el)
            tab_num = tab_num + 1
            if el.caption and el.caption.long and #el.caption.long > 0 then
                local block = el.caption.long[1]
                if block.content then
                    block.content:insert(1, pandoc.Space())
                    block.content:insert(1, pandoc.Strong{
                        pandoc.Str("Table " .. tab_num .. ":")
                    })
                end
            end
            return el
        end,

        Link = function(el)
            local rtype = el.attributes["reference-type"]
            if not rtype then return nil end
            local ref = el.attributes["reference"] or ""

            if rtype == "eqref" then
                local num = eq_labels[ref]
                if num then
                    el.content = {pandoc.Str("(" .. num .. ")")}
                end
                return el
            end

            if rtype == "ref+label" or rtype == "ref+Label" then
                local kind = ref_kind(ref)
                if kind then
                    local map = rtype == "ref+Label" and AUTOREF_UPPER
                                                      or AUTOREF_LOWER
                    local prefix = map[kind]
                    if prefix then
                        table.insert(el.content, 1, pandoc.Str(prefix))
                    end
                end
                -- Fix unresolved refs: pandoc renders them as [label]
                local txt = pandoc.utils.stringify(el.content)
                local escaped = ref:gsub("([%-%.%+])", "%%%1")
                if txt:match("%[" .. escaped .. "%]") then
                    el.content = {pandoc.Str(txt:gsub("%[" .. escaped .. "%]", "link"))}
                end
                return el
            end
        end,
    },
}
