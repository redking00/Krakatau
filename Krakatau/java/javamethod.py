import collections
from functools import partial

from ..ssa import objtypes
from .. import graph_util
from ..namegen import NameGen, LabelGen
from ..verifier.descriptors import parseMethodDescriptor

from . import ast, ast2, boolize
from . import graphproxy, structuring, astgen

class DeclInfo(object):
    __slots__ = "declScope scope defs".split()
    def __init__(self):
        self.declScope = self.scope = None
        self.defs = []

def findVarDeclInfo(root, predeclared):
    info = collections.OrderedDict()
    def visit(scope, expr):
        for param in expr.params:
            visit(scope, param)

        if expr.isLocalAssign():
            left, right = expr.params
            info[left].defs.append(right)
        elif isinstance(expr, (ast.Local, ast.Literal)):
            #this would be so much nicer if we had Ordered defaultdicts
            info.setdefault(expr, DeclInfo())
            info[expr].scope = ast.StatementBlock.join(info[expr].scope, scope)

    def visitDeclExpr(scope, expr):
        info.setdefault(expr, DeclInfo())
        assert(scope is not None and info[expr].declScope is None)
        info[expr].declScope = scope

    for expr in predeclared:
        visitDeclExpr(root, expr)

    stack = [(root,root)]
    while stack:
        scope, stmt = stack.pop()
        if isinstance(stmt, ast.StatementBlock):
            stack.extend((stmt,sub) for sub in stmt.statements)
        else:
            stack.extend((subscope,subscope) for subscope in stmt.getScopes())
            #temp hack
            if stmt.expr is not None:
                visit(scope, stmt.expr)
            if isinstance(stmt, ast.TryStatement):
                for catchdecl, body in stmt.pairs:
                    visitDeclExpr(body, catchdecl.local)
    return info

def reverseBoolExpr(expr):
    assert(expr.dtype == objtypes.BoolTT)
    if isinstance(expr, ast.BinaryInfix):
        symbols = "== != < >= > <=".split()
        floatts = (objtypes.FloatTT, objtypes.DoubleTT)
        if expr.opstr in symbols:
            sym2 = symbols[symbols.index(expr.opstr) ^ 1]
            left, right = expr.params
            #be sure not to reverse floating point comparisons since it's not equivalent for NaN
            if expr.opstr in symbols[:2] or (left.dtype not in floatts and right.dtype not in floatts):
                return ast.BinaryInfix(sym2, (left,right), objtypes.BoolTT)
    elif isinstance(expr, ast.UnaryPrefix) and expr.opstr == '!':
        return expr.params[0]
    return ast.UnaryPrefix('!', expr)

def getSubscopeIter(root):
    stack = [root]
    while stack:
        scope = stack.pop()
        if isinstance(scope, ast.StatementBlock):
            stack.extend(scope.statements)
            yield scope
        else:
            stack.extend(scope.getScopes())

def mayBreakTo(root, forbidden):
    assert(None not in forbidden)
    for scope in getSubscopeIter(root):
        if scope.jumpKey in forbidden:
            #We return true if scope has forbidden jump and is reachable
            #We assume there is no unreachable code, so in order for a scope
            #jump to be unreachable, it must end in a return, throw, or a
            #compound statement, all of which are not reachable or do not
            #break out of the statement. We omit adding last.breakKey to
            #forbidden since it should always match scope.jumpKey anyway
            if not scope.statements:
                return True
            last = scope.statements[-1]
            if not last.getScopes():
                if not isinstance(last, (ast.ReturnStatement, ast.ThrowStatement)):
                    return True
            else:
                #If and switch statements may allow fallthrough
                #A while statement with condition may break implicitly
                if isinstance(last, ast.IfStatement) and len(last.getScopes()) == 1:
                    return True
                if isinstance(last, ast.SwitchStatement) and not last.hasDefault():
                    return True
                if isinstance(last, ast.WhileStatement) and last.expr != ast.Literal.TRUE:
                    return True

                if not isinstance(last, ast.WhileStatement):
                    for sub in last.getScopes():
                        assert(sub.breakKey == last.breakKey == scope.jumpKey)
    return False

def replaceKeys(top, replace):
    assert(None not in replace)
    get = lambda k:replace.get(k,k)

    if top.getScopes():
        if isinstance(top, ast.StatementBlock) and get(top.breakKey) is None:
            #breakkey can be None with non-None jumpkey when we're a scope in a switch statement that falls through
            #and the end of the switch statement is unreachable
            assert(get(top.jumpKey) is None or not top.labelable)

        top.breakKey = get(top.breakKey)
        if isinstance(top, ast.StatementBlock):
            top.jumpKey = get(top.jumpKey)
            for item in top.statements:
                replaceKeys(item, replace)
        else:
            for scope in top.getScopes():
                replaceKeys(scope, replace)

NONE_SET = frozenset([None])
def _preorder(scope, func):
    newitems = []
    for i, item in enumerate(scope.statements):
        for sub in item.getScopes():
            _preorder(sub, func)

        val = func(scope, item)
        vals = [item] if val is None else val
        newitems.extend(vals)
    scope.statements = newitems

def _fixObjectCreations(scope, item):
    '''Combines new/invokeinit pairs into Java constructor calls'''

    #Thanks to the copy propagation pass prior to AST generation, as well as the fact that
    #unitialized types never merge, we can safely assume there are no copies to worry about
    expr = item.expr
    if isinstance(expr, ast.Assignment):
        left, right = expr.params
        if isinstance(right, ast.Dummy) and right.isNew:
            return [] #remove item

    elif isinstance(expr, ast.MethodInvocation) and expr.name == '<init>':
        left = expr.params[0]
        newexpr = ast.ClassInstanceCreation(ast.TypeName(left.dtype), expr.tts[1:], expr.params[1:])
        item.expr = ast.Assignment(left, newexpr)

def _pruneRethrow_cb(item):
    '''Convert try{A} catch(T e) {throw t;} to {A}'''
    while item.pairs:
        decl, body = item.pairs[-1]
        caught, lines = decl.local, body.statements

        if len(lines) == 1:
            line = lines[0]
            if isinstance(line, ast.ThrowStatement) and line.expr == caught:
                item.pairs = item.pairs[:-1]
                continue
        break
    if not item.pairs:
        new = item.tryb
        assert(new.breakKey == item.breakKey)
        assert(new.continueKey == item.continueKey)
        assert(not new.labelable)
        new.labelable = True
        return new
    return item

def _pruneIfElse_cb(item):
    '''Convert if(A) {B} else {} to if(A) {B}'''
    if len(item.scopes) > 1:
        tblock, fblock = item.scopes

        #if true block is empty, swap it with false so we can remove it
        if not tblock.statements and tblock.doesFallthrough():
            item.expr = reverseBoolExpr(item.expr)
            tblock, fblock = fblock, tblock
            item.scopes = tblock, fblock

        if not fblock.statements and fblock.doesFallthrough():
            item.scopes = tblock,
        # If cond is !(x), reverse it back to simplify cond
        elif isinstance(item.expr, ast.UnaryPrefix) and item.expr.opstr == '!':
            item.expr = reverseBoolExpr(item.expr)
            item.scopes = fblock, tblock
    return item

def _whileCondition_cb(item):
    '''Convert while(true) {if(A) {B break;} else {C} D} to while(!A) {{C} D} {B}'''
    failure = [], item #what to return if we didn't inline
    body = item.getScopes()[0]
    if not body.statements or not isinstance(body.statements[0], ast.IfStatement):
        return failure
    if item.expr != ast.Literal.TRUE: #don't use && conditions for now
        return failure

    head = body.statements[0]
    cond = head.expr
    trueb, falseb = (head.getScopes() + (None,))[:2]

    #Make sure it doesn't continue the loop or break out of the if statement
    badjumps1 = frozenset([head.breakKey, item.continueKey]) - NONE_SET
    if mayBreakTo(trueb, badjumps1):
        if falseb is not None and not mayBreakTo(falseb, badjumps1):
            cond = reverseBoolExpr(cond)
            trueb, falseb = falseb, trueb
        else:
            return failure
    assert(not mayBreakTo(trueb, badjumps1))

    #If break body is nontrival, we can't insert this after the end of the loop unless
    #We're sure that nothing else in the loop breaks out
    badjumps2 = frozenset([item.breakKey]) - NONE_SET
    trivial = not trueb.statements and trueb.jumpKey == item.breakKey
    if not trivial:
        restloop = [falseb] if falseb is not None else []
        restloop += body.statements[1:]
        if body.jumpKey == item.breakKey or any(mayBreakTo(s, badjumps2) for s in restloop):
            return failure

    #Now inline everything
    item.expr = reverseBoolExpr(cond)
    if falseb is None:
        body.statements.pop(0)
    else:
        body.statements[0] = falseb
        falseb.labelable = True
    trueb.labelable = True

    if item.breakKey is None: #Make sure to maintain invariant that bkey=None -> jkey=None
        assert(trueb.doesFallthrough())
        trueb.jumpKey = trueb.breakKey = None
    trueb.breakKey = item.breakKey
    assert(trueb.continueKey is not None)
    if not trivial:
        item.breakKey = trueb.continueKey

    #Trueb doesn't break to head.bkey but there might be unreacahble jumps, so we replace
    #it too. We don't replace item.ckey because it should never appear, even as an
    #unreachable jump
    replaceKeys(trueb, {head.breakKey:trueb.breakKey, item.breakKey:trueb.breakKey})
    return [item], trueb

def _simplifyBlocksSub(scope, item, isLast):
    rest = []
    if isinstance(item, ast.TryStatement):
        item = _pruneRethrow_cb(item)
    elif isinstance(item, ast.IfStatement):
        item = _pruneIfElse_cb(item)
    elif isinstance(item, ast.WhileStatement):
        rest, item = _whileCondition_cb(item)

    if isinstance(item, ast.StatementBlock):
        assert(item.breakKey is not None or item.jumpKey is None)
        #If bkey is None, it can't be broken to
        #If contents can also break to enclosing scope, it's always safe to inline
        bkey = item.breakKey
        if bkey is None or (bkey == scope.breakKey and scope.labelable):
            rest, item.statements = rest + item.statements, []

        for sub in item.statements[:]:
            if sub.getScopes() and sub.breakKey != bkey and mayBreakTo(sub, frozenset([bkey])):
                break
            rest.append(item.statements.pop(0))

        if not item.statements:
            if item.jumpKey != bkey:
                assert(isLast)
                scope.jumpKey = item.jumpKey
                assert(scope.breakKey is not None or scope.jumpKey is None)
            return rest
    return rest + [item]

def _simplifyBlocks(scope):
    newitems = []
    for item in reversed(scope.statements):
        isLast = not newitems #may be true if all subsequent items pruned
        if isLast and item.getScopes():
            if item.breakKey != scope.jumpKey:# and item.breakKey is not None:
                # print 'sib replace', scope, item, item.breakKey, scope.jumpKey
                replaceKeys(item, {item.breakKey: scope.jumpKey})

        for sub in reversed(item.getScopes()):
            _simplifyBlocks(sub)
        vals = _simplifyBlocksSub(scope, item, isLast)
        newitems += reversed(vals)
    scope.statements = newitems[::-1]

def _setScopeParents(scope):
    for item in scope.statements:
        for sub in item.getScopes():
            sub.bases = scope.bases + (sub,)
            _setScopeParents(sub)

def _replaceExpressions(scope, item, rdict):
    #Must be done before local declarations are created since it doesn't touch/remove them
    if item.expr is not None:
        item.expr = item.expr.replaceSubExprs(rdict)
    #remove redundant assignments i.e. x=x;
    if isinstance(item.expr, ast.Assignment):
        assert(isinstance(item, ast.ExpressionStatement))
        left, right = item.expr.params
        if left == right:
            return []
    return [item]

def _mergeVariables(root, predeclared):
    _setScopeParents(root)
    info = findVarDeclInfo(root, predeclared)

    lvars = [expr for expr in info if isinstance(expr, ast.Local)]
    forbidden = set()
    #If var has any defs which aren't a literal or local, mark it as a leaf node (it can't be merged into something)
    for var in lvars:
        if not all(isinstance(expr, (ast.Local, ast.Literal)) for expr in info[var].defs):
            forbidden.add(var)
        elif info[var].declScope is not None:
            forbidden.add(var)

    sccs = graph_util.tarjanSCC(lvars, lambda var:([] if var in forbidden else info[var].defs))
    #the sccs will be in topolgical order
    varmap = {}
    for scc in sccs:
        if forbidden.isdisjoint(scc):
            alldefs = []
            for expr in scc:
                for def_ in info[expr].defs:
                    if def_ not in scc:
                        alldefs.append(varmap[def_])
            if len(set(alldefs)) == 1:
                target = alldefs[0]
                if all(var.dtype == target.dtype for var in scc):
                    scope = ast.StatementBlock.join(*(info[var].scope for var in scc))
                    scope = ast.StatementBlock.join(scope, info[target].declScope) #scope is unchanged if declScope is none like usual
                    if info[target].declScope is None or info[target].declScope == scope:
                        for var in scc:
                            varmap[var] = target
                        info[target].scope = ast.StatementBlock.join(scope, info[target].scope)
                        continue
        #fallthrough if merging is impossible
        for var in scc:
            varmap[var] = var
            if len(info[var].defs) > 1:
                forbidden.add(var)
    _preorder(root, partial(_replaceExpressions, rdict=varmap))
    _preorder(root, partial(_replaceExpressions, rdict=varmap))

def _inlineVariables(root):
    #first find all variables with a single def and use
    defs = collections.defaultdict(list)
    uses = collections.defaultdict(int)

    def visitExprFindDefs(expr):
        if expr.isLocalAssign():
            defs[expr.params[0]].append(expr)
        elif isinstance(expr, ast.Local):
            uses[expr] += 1

    def visitFindDefs(scope, item):
        if item.expr is not None:
            stack = [item.expr]
            while stack:
                expr = stack.pop()
                visitExprFindDefs(expr)
                stack.extend(expr.params)

    _preorder(root, visitFindDefs)
    #These should have 2 uses since the initial assignment also counts
    replacevars = {k for k,v in defs.items() if len(v)==1 and uses[k]==2 and k.dtype == v[0].params[1].dtype}

    #Avoid reordering past expressions that potentially have side effects or depend on external state
    oktypes = ast.BinaryInfix, ast.Local, ast.Literal, ast.Parenthesis, ast.TypeName, ast.UnaryPrefix
    def isBarrier(expr):
        if not isinstance(expr, oktypes):
            return True
        #check for division by 0. If it's a float or dividing by nonzero literal, it's ok
        elif isinstance(expr, ast.BinaryInfix) and expr.opstr in ('/','%'):
            if expr.dtype not in (objtypes.FloatTT, objtypes.DoubleTT):
                divisor = expr.params[-1]
                if not isinstance(divisor, ast.Literal) or divisor.val == 0:
                    return True
        assert(not isinstance(expr, ast.BinaryInfix) or expr.opstr not in ('&&','||'))
        return False

    def doReplacement(item, pairs):
        old, new = item.expr.params
        assert(isinstance(old, ast.Local) and old.dtype == new.dtype)
        stack = [(True, (True, item2, expr)) for item2, expr in reversed(pairs) if expr is not None]
        while stack:
            recurse, args = stack.pop()

            if recurse:
                canReplace, parent, expr = args
                stack.append((False, expr))

                #For ternaries, we don't want to replace into the conditionally
                #evaluated part, but we still need to check those parts for
                #barriers
                if isinstance(expr, ast.Ternary):
                    stack.append((True, (False, expr, expr.params[2])))
                    stack.append((True, (False, expr, expr.params[1])))
                    stack.append((True, (canReplace, expr, expr.params[0])))
                #For assignments, we unroll the LHS arguments, because if assigning
                #to an array or field, we don't want that to serve as a barrier
                elif isinstance(expr, ast.Assignment):
                    left, right = expr.params
                    stack.append((True, (canReplace, expr, right)))
                    if isinstance(left, (ast.ArrayAccess, ast.FieldAccess)):
                        for param in reversed(left.params):
                            stack.append((True, (canReplace, left, param)))
                    else:
                        assert(isinstance(left, ast.Local))
                else:
                    for param in reversed(expr.params):
                        stack.append((True, (canReplace, expr, param)))

                if expr == old:
                    if canReplace:
                        if isinstance(parent, ast.JavaExpression):
                            params = parent.params = list(parent.params)
                            params[params.index(old)] = new
                        else: #replacing in a top level statement
                            assert(parent.expr == old)
                            parent.expr = new
                    return canReplace
            else:
                expr = args
                if isBarrier(expr):
                    return False
        return False

    def visitReplace(scope):
        newstatements = []
        for item in reversed(scope.statements):
            for sub in item.getScopes():
                visitReplace(sub)

            if isinstance(item.expr, ast.Assignment) and item.expr.params[0] in replacevars:
                expr_roots = []
                for item2 in newstatements:
                    #Don't inline into a while condition as it may be evaluated more than once
                    if not isinstance(item2, ast.WhileStatement):
                        expr_roots.append((item2, item2.expr))
                    if item2.getScopes():
                        break
                success = doReplacement(item, expr_roots)
                if success:
                    continue
            newstatements.insert(0, item)
        scope.statements = newstatements
    visitReplace(root)

def _createDeclarations(root, predeclared):
    _setScopeParents(root)
    info = findVarDeclInfo(root, predeclared)
    localdefs = collections.defaultdict(list)
    newvars = [var for var in info if isinstance(var, ast.Local) and info[var].declScope is None]
    remaining = set(newvars)

    #The compiler treats statements as if they can throw any exception at any time, so
    #it may think variables are not definitely assigned even when they really are.
    #Therefore, we give an unused initial value to every variable declaration
    #TODO - find a better way to handle this
    _init_d = {objtypes.BoolTT: ast.Literal.FALSE,
            objtypes.IntTT: ast.Literal.ZERO,
            objtypes.FloatTT: ast.Literal.FZERO,
            objtypes.DoubleTT: ast.Literal.DZERO}
    def mdVisitVarUse(var):
        decl = ast.VariableDeclarator(ast.TypeName(var.dtype), var)
        right = _init_d.get(var.dtype, ast.Literal.NULL)
        localdefs[info[var].scope].append( ast.LocalDeclarationStatement(decl, right) )
        remaining.remove(var)

    def mdVisitScope(scope):
        if isinstance(scope, ast.StatementBlock):
            for i,stmt in enumerate(scope.statements):
                if isinstance(stmt, ast.ExpressionStatement):
                    if isinstance(stmt.expr, ast.Assignment):
                        var, right = stmt.expr.params
                        if var in remaining and scope == info[var].scope:
                            decl = ast.VariableDeclarator(ast.TypeName(var.dtype), var)
                            new = ast.LocalDeclarationStatement(decl, right)
                            scope.statements[i] = new
                            remaining.remove(var)
                if stmt.expr is not None:
                    top = stmt.expr
                    for expr in top.postFlatIter():
                        if expr in remaining:
                            mdVisitVarUse(expr)
                for sub in stmt.getScopes():
                    mdVisitScope(sub)

    mdVisitScope(root)
    # print remaining
    assert(not remaining)
    assert(None not in localdefs)
    for scope, ldefs in localdefs.items():
        scope.statements = ldefs + scope.statements

def _simplifyExpressions(expr):
    truefalse = (ast.Literal.TRUE, ast.Literal.FALSE)
    expr.params = map(_simplifyExpressions, expr.params)

    if isinstance(expr, ast.Ternary):
        cond, val1, val2 = expr.params
        if (val1, val2) == truefalse:
            expr = cond
        elif (val2, val1) == truefalse:
            expr = reverseBoolExpr(cond)
        elif isinstance(cond, ast.UnaryPrefix): # (!x)?y:z -> x?z:y
            expr.params = reverseBoolExpr(cond), val2, val1

    if isinstance(expr, ast.BinaryInfix) and expr.opstr in ('==', '!='):
        v1, v2 = expr.params
        if v1 in truefalse:
            v2, v1 = v1, v2
        if v2 in truefalse:
            match = (v2 == ast.Literal.TRUE) == (expr.opstr == '==')
            expr = v1 if match else reverseBoolExpr(v1)
        # Fix Yoda comparisons (if(null == x), etc.
        elif isinstance(v1, ast.Literal):
            expr.params = v2, v1
    return expr

def _createTernaries(scope, item):
    if isinstance(item, ast.IfStatement) and len(item.getScopes()) == 2:
        block1, block2 = item.getScopes()

        if (len(block1.statements) == len(block2.statements) == 1) and block1.jumpKey == block2.jumpKey:
            s1, s2 = block1.statements[0], block2.statements[0]
            e1, e2 = s1.expr, s2.expr

            if isinstance(s1, ast.ReturnStatement) and isinstance(s2, ast.ReturnStatement):
                expr = None if e1 is None else ast.Ternary(item.expr, e1, e2)
                item = ast.ReturnStatement(expr, s1.tt)
            if isinstance(s1, ast.ExpressionStatement) and isinstance(s2, ast.ExpressionStatement):
                if isinstance(e1, ast.Assignment) and isinstance(e2, ast.Assignment):
                    # if e1.params[0] == e2.params[0] and max(e1.params[1].complexity(), e2.params[1].complexity()) <= 1:
                    if e1.params[0] == e2.params[0]:
                        expr = ast.Ternary(item.expr, e1.params[1], e2.params[1])
                        temp = ast.ExpressionStatement(ast.Assignment(e1.params[0], expr))

                        if not block1.doesFallthrough():
                            assert(not block2.doesFallthrough())
                            item = ast.StatementBlock(item.func, item.continueKey, item.breakKey, [temp], block1.jumpKey)
                        else:
                            item = temp
    if item.expr is not None:
        item.expr = _simplifyExpressions(item.expr)
    return [item]

def _fixExprStatements(scope, item, namegen):
    if isinstance(item, ast.ExpressionStatement):
        if not isinstance(item.expr, (ast.Assignment, ast.ClassInstanceCreation, ast.MethodInvocation, ast.Dummy)):
            right = item.expr
            left = ast.Local(right.dtype, lambda expr:namegen.getPrefix('dummy'))
            decl = ast.VariableDeclarator(ast.TypeName(left.dtype), left)
            item = ast.LocalDeclarationStatement(decl, right)
    return [item]

def _addCastsAndParens(scope, item, env):
    item.addCastsAndParens(env)

def _chooseJump(choices):
    for b, t in choices:
        if b is None:
            return b, t
    for b, t in choices:
        if b.label is not None:
            return b, t
    return choices[0]

def _generateJumps(scope, targets=collections.OrderedDict(), fallthroughs=NONE_SET, dryRun=False):
    assert(None in fallthroughs)
    #breakkey can be None with non-None jumpkey when we're a scope in a switch statement that falls through
    #and the end of the switch statement is unreachable
    assert(scope.breakKey is not None or scope.jumpKey is None or not scope.labelable)
    if scope.jumpKey not in fallthroughs:
        assert(not scope.statements or not isinstance(scope.statements[-1], (ast.ReturnStatement, ast.ThrowStatement)))
        vals = [k for k,v in targets.items() if v == scope.jumpKey]
        assert(vals)
        jump = _chooseJump(vals)
        if not dryRun:
            scope.statements.append(ast.JumpStatement(*jump))

    for item in reversed(scope.statements):
        if not item.getScopes():
            fallthroughs = NONE_SET
            continue

        if isinstance(item, ast.WhileStatement):
            fallthroughs = frozenset([None, item.continueKey])
        else:
            fallthroughs |= frozenset([item.breakKey])

        newtargets = targets.copy()
        if isinstance(item, ast.WhileStatement):
            newtargets[None, True] = item.continueKey
            newtargets[item, True] = item.continueKey
        if isinstance(item, (ast.WhileStatement, ast.SwitchStatement)):
            newtargets[None, False] = item.breakKey
        newtargets[item, False] = item.breakKey

        for subscope in reversed(item.getScopes()):
            _generateJumps(subscope, newtargets, fallthroughs, dryRun=dryRun)
            if isinstance(item, ast.SwitchStatement):
                fallthroughs = frozenset([None, subscope.continueKey])
        fallthroughs = frozenset([None, item.continueKey])

def _pruneVoidReturn(scope):
    if scope.statements:
        last = scope.statements[-1]
        if isinstance(last, ast.ReturnStatement) and last.expr is None:
            scope.statements.pop()

def generateAST(method, graph, forbidden_identifiers):
    env = method.class_.env
    namegen = NameGen(forbidden_identifiers)
    class_ = method.class_
    inputTypes = parseMethodDescriptor(method.descriptor, unsynthesize=False)[0]
    tts = objtypes.verifierToSynthetic_seq(inputTypes)

    if graph is not None:
        entryNode, nodes = graphproxy.createGraphProxy(graph)
        if not method.static:
            entryNode.invars[0].name = 'this'

        setree = structuring.structure(entryNode, nodes, (method.name == '<clinit>'))
        ast_root, varinfo = astgen.createAST(method, graph, setree, namegen)

        argsources = [varinfo.var(entryNode, var) for var in entryNode.invars]
        disp_args = argsources if method.static else argsources[1:]
        for expr, tt in zip(disp_args, tts):
            expr.dtype = tt

        decls = [ast.VariableDeclarator(ast.TypeName(expr.dtype), expr) for expr in disp_args]
        ################################################################################################
        ast_root.bases = (ast_root,) #needed for our setScopeParents later

        # print ast_root.print_()
        assert(_generateJumps(ast_root, dryRun=True) is None)
        _preorder(ast_root, _fixObjectCreations)
        boolize.boolizeVars(ast_root, argsources)
        _simplifyBlocks(ast_root)
        assert(_generateJumps(ast_root, dryRun=True) is None)

        _mergeVariables(ast_root, argsources)
        _preorder(ast_root, _createTernaries)
        _inlineVariables(ast_root)
        _simplifyBlocks(ast_root)
        _preorder(ast_root, _createTernaries)
        _inlineVariables(ast_root)
        _simplifyBlocks(ast_root)

        _createDeclarations(ast_root, argsources)
        _preorder(ast_root, partial(_fixExprStatements, namegen=namegen))
        _preorder(ast_root, partial(_addCastsAndParens, env=env))
        _generateJumps(ast_root)
        _pruneVoidReturn(ast_root)
    else: #abstract or native method
        ast_root = None
        argsources = [ast.Local(tt, lambda expr:namegen.getPrefix('arg')) for tt in tts]
        decls = [ast.VariableDeclarator(ast.TypeName(expr.dtype), expr) for expr in argsources]

    flags = method.flags - set(['BRIDGE','SYNTHETIC','VARARGS'])
    if method.name == '<init>': #More arbtirary restrictions. Yay!
        flags = flags - set(['ABSTRACT','STATIC','FINAL','NATIVE','STRICTFP','SYNCHRONIZED'])

    flagstr = ' '.join(map(str.lower, sorted(flags)))
    inputTypes, returnTypes = parseMethodDescriptor(method.descriptor, unsynthesize=False)
    ret_tt = objtypes.verifierToSynthetic(returnTypes[0]) if returnTypes else ('.void',0)
    return ast2.MethodDef(class_, flagstr, method.name, ast.TypeName(ret_tt), decls, ast_root)