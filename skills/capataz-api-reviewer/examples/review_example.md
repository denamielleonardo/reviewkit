# Example Review: Bad Implementation

## Code Snippet (Infrastructure Layer)
```csharp
public void Configure(EntityTypeBuilder<AuditLog> builder)
{
    builder.HasKey(x => x.Id);
    // Missing table name mapping
    // Missing column constraints
}
```

## Review Output

### Overall Verdict
**NOT PRODUCTION-READY**

### Critical Issues (Must Fix)
1. **Implicit Table Naming**: You failed to use `builder.ToTable()`. In this project, we explicitly map tables to constants in `ReferenceValues.TableNames`. Letting EF Core decide the table name is a recipe for migration chaos.
2. **Missing Constraints**: An audit log table without indexed timestamps or required properties (`Message`, `UserId`) is useless for performance and data integrity.

### Architecture Alignment Score
**Score: 2/10**
You ignored the Persistence layer patterns established in `ReferenceConfigration.cs`.

---

# Example Review: Good Implementation

## Code Snippet (Application Layer)
```csharp
public class GetUserQueryHandler : IRequestHandler<GetUserQuery, Response<UserDto>>
{
    private readonly IApplicationDbContext _context;
    private readonly IMapper _mapper;

    public async Task<Response<UserDto>> Handle(GetUserQuery request, CancellationToken ct)
    {
        var user = await _context.UserProfiles
            .ProjectTo<UserDto>(_mapper.ConfigurationProvider)
            .FirstOrDefaultAsync(x => x.Id == request.Id, ct);

        return user != null 
            ? Response<UserDto>.Success(user) 
            : throw new NotFoundException(nameof(User), request.Id);
    }
}
```

### Overall Verdict
**WELL-DESIGNED**

### Performance Analysis
Efficient use of `ProjectTo` ensures we don't fetch entire entities into memory. The query is clean and direct.
